"""NOX_KIT 2026-11-18d — durable local file publication helpers.

Only the caller's explicit files are touched. A failed atomic write never removes
the old destination. Group publication records intent before the first rename.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time


class PublicationError(RuntimeError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write(path: Path, data, attempts: int = 5) -> None:
    """Use an exclusive, unique temporary file beside the destination."""
    path = Path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    tmp = path.with_name("." + path.name + "." + secrets.token_hex(12) + ".tmp")
    try:
        with tmp.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(max(1, attempts)):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 == max(1, attempts):
                    raise
                time.sleep(0.15 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_copy(source: Path, destination: Path) -> None:
    """Stream a verified copy, then replace; do not load MP3/WAV into RAM."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name("." + destination.name + "." + secrets.token_hex(12) + ".tmp")
    try:
        before = source.stat()
        with source.open("rb") as src, tmp.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PublicationError("исходный файл изменился во время копирования: " + str(source))
        if digest(tmp) != digest(source):
            raise PublicationError("контрольная сумма копии не совпала: " + str(source))
        for attempt in range(5):
            try:
                os.replace(tmp, destination)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.15 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class Publication:
    """Publish several staged files with a persistent rollback journal.

    Old files are renamed beside themselves (no duplicate audio allocation).
    Call commit only after all associated metadata is durably published. The
    transaction directory must be recovered before a subsequent writer starts.
    """
    def __init__(self, journal_dir: Path, pairs, *, allowed_root: Path):
        self.allowed_root = Path(allowed_root).resolve()
        self.directory = Path(journal_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.id = secrets.token_hex(16)
        self.path = self.directory / (self.id + ".json")
        self.doc = {"id": self.id, "state": "prepared", "root": str(self.allowed_root), "items": []}
        seen = set()
        for source, destination in pairs:
            source = Path(source).resolve() if source is not None else None
            destination = Path(destination).absolute()
            try:
                destination.resolve().relative_to(self.allowed_root)
            except ValueError as e:
                raise PublicationError("путь публикации вне проекта") from e
            if destination.is_symlink() or destination in seen:
                raise PublicationError("повторный путь или ссылка в публикации")
            seen.add(destination)
            if source is not None and (not source.is_file() or source == destination):
                raise PublicationError("некорректный подготовленный файл")
            old_hash = digest(destination) if destination.exists() else None
            self.doc["items"].append({
                "source": str(source) if source is not None else None,
                "destination": str(destination),
                "backup": str(destination.with_name("." + destination.name + ".previous-" + self.id)),
                "old_hash": old_hash, "new_hash": digest(source) if source is not None else None,
            })
        self._save()

    def _save(self):
        atomic_write(self.path, json.dumps(self.doc, ensure_ascii=False, indent=2))

    def publish(self):
        for item in self.doc["items"]:
            if item["source"] is not None and digest(Path(item["source"])) != item["new_hash"]:
                raise PublicationError("подготовленный файл изменён до публикации: " + item["source"])
        self.doc["state"] = "publishing"
        self._save()
        try:
            for item in self.doc["items"]:
                dst, backup = Path(item["destination"]), Path(item["backup"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                if item["source"] is not None and digest(Path(item["source"])) != item["new_hash"]:
                    raise PublicationError("подготовленный файл изменён перед заменой: " + item["source"])
                actual = digest(dst) if dst.exists() else None
                if actual != item["old_hash"] or backup.exists():
                    raise PublicationError("целевой файл изменился перед публикацией: " + str(dst))
                if dst.exists():
                    os.replace(dst, backup)
                if item["source"] is not None:
                    os.replace(item["source"], dst)
                    if digest(dst) != item["new_hash"]:
                        raise PublicationError("контрольная сумма опубликованного файла не совпала")
            self.doc["state"] = "published"
            self._save()
        except BaseException:
            self.rollback()
            raise

    def commit(self):
        if self.doc["state"] != "published":
            raise PublicationError("публикация не завершена")
        self.doc["state"] = "committed"
        try:
            self._save()
        except BaseException:
            self.doc["state"] = "published"
            raise
        # A cleanup error is not a failed commit: the journal is authoritative.
        for item in self.doc["items"]:
            try:
                Path(item["backup"]).unlink(missing_ok=True)
            except OSError:
                pass

    def rollback(self):
        # A corrupt backup is discovered before any of the other files change.
        for item in self.doc["items"]:
            dst, backup = Path(item["destination"]), Path(item["backup"])
            if backup.exists():
                if digest(backup) != item["old_hash"]:
                    raise PublicationError("повреждена резервная версия: " + str(backup))
                if dst.exists() and digest(dst) not in (item["new_hash"], item["old_hash"]):
                    raise PublicationError("целевой файл изменён после сбоя: " + str(dst))
            elif item["old_hash"] is None and dst.exists():
                if digest(dst) != item["new_hash"]:
                    raise PublicationError("новый целевой файл изменён после сбоя: " + str(dst))
            elif item["old_hash"] is not None and (not dst.exists() or digest(dst) != item["old_hash"]):
                raise PublicationError("не найдена исходная версия для отката: " + str(dst))
        failures = []
        for item in reversed(self.doc["items"]):
            dst, backup = Path(item["destination"]), Path(item["backup"])
            try:
                if backup.exists():
                    if digest(backup) != item["old_hash"]:
                        raise PublicationError("повреждена резервная версия")
                    if dst.exists() and digest(dst) not in (item["new_hash"], item["old_hash"]):
                        raise PublicationError("после сбоя целевой файл изменён другим процессом")
                    os.replace(backup, dst)
                elif item["old_hash"] is None and dst.exists():
                    if digest(dst) != item["new_hash"]:
                        raise PublicationError("новый целевой файл изменён другим процессом")
                    dst.unlink()
                elif item["old_hash"] is not None and (not dst.exists() or digest(dst) != item["old_hash"]):
                    raise PublicationError("не найдена исходная версия для отката")
            except (OSError, PublicationError) as exc:
                failures.append(str(dst) + ": " + str(exc))
        self.doc["state"] = "rollback_failed" if failures else "rolled_back"
        self.doc["errors"] = failures
        self._save()
        if failures:
            raise PublicationError("откат требует восстановления по журналу " + str(self.path) + ": " + "; ".join(failures))


def recover_publications(journal_dir: Path, allowed_root: Path) -> list:
    """Roll back interrupted publications; never guess after external changes."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return []
    results = []
    root = Path(allowed_root).resolve()
    for path in sorted(journal_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("state") in ("committed", "rolled_back"):
                continue
            if Path(doc["root"]).resolve() != root or path.stem != doc["id"]:
                raise PublicationError("журнал относится к другому проекту")
            for item in doc["items"]:
                for key in ("destination", "backup"):
                    Path(item[key]).resolve().relative_to(root)
                dst = Path(item["destination"])
                if Path(item["backup"]) != dst.with_name("." + dst.name + ".previous-" + doc["id"]):
                    raise PublicationError("неверный путь резервной версии")
            tx = object.__new__(Publication)
            tx.allowed_root, tx.directory, tx.id, tx.path, tx.doc = root, journal_dir, doc["id"], path, doc
            tx.rollback()
            results.append(str(path))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise PublicationError("не удалось прочитать журнал публикации " + str(path)) from exc
    return results
