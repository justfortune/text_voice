"""Durable, reversible Studio file operations (stdlib, Windows and POSIX).

The manifest and both document revisions are durable before any source is moved.
An unfinished operation is rolled back on restart; a completed operation keeps
its files and snapshots for Undo. There is deliberately no unlink fallback.
All paths are confined to the book's studio directory and all moves stay on one
volume. This is not a mechanism for arbitrary paths supplied by an HTTP client.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid


class TransactionError(RuntimeError):
    pass


def digest(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sync_dir(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path: Path, data: bytes):
    """One uniquely named, fsynced temporary; never reuse another writer's tmp."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                _sync_dir(path.parent)
                return
            except OSError:
                if attempt == 7:
                    raise
                time.sleep(min(.04 * (2 ** attempt), .5))
    finally:
        # A locked temporary must not hide the original publication error.
        # It is uniquely named and never becomes an authoritative document.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


@contextlib.contextmanager
def directory_lock(root: Path):
    """Process lock, automatically released if the process exits."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "operations.lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0"); f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise TransactionError("другая студия меняет файлы этой книги") from e
            try:
                yield
            finally:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise TransactionError("другая студия меняет файлы этой книги") from e
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


class FileTransactions:
    def __init__(self, studio_dir: str | Path):
        self.root = Path(studio_dir).resolve()
        self.directory = self.root / "transactions"

    def path(self, value: str | Path) -> Path:
        raw = Path(value)
        if not raw.is_absolute():
            raw = self.root / raw
        p = raw.resolve()
        if self.root not in p.parents or raw.is_symlink():
            raise TransactionError(f"путь операции за пределами студии: {value}")
        # A symlinked ancestor is also inappropriate for a media transaction.
        q = raw.absolute()
        while q != self.root and self.root in q.parents:
            if q.is_symlink():
                raise TransactionError(f"символическая ссылка в пути операции: {q}")
            q = q.parent
        return p

    def relative(self, path: str | Path) -> str:
        return self.path(path).relative_to(self.root).as_posix()

    def manifest(self, op_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", str(op_id)):
            raise TransactionError("повреждён идентификатор файловой операции")
        p = self.directory / op_id / "operation.json"
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TransactionError(f"не читается журнал {p}: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("id") != op_id:
            raise TransactionError(f"повреждён журнал {p}")
        return doc

    def _record(self, record):
        atomic_json(self.directory / record["id"] / "operation.json", record)

    def _snapshot(self, record, rel, which):
        entry = next(x for x in record["documents"] if x["path"] == rel)
        expected = entry[which + "_sha"]
        if expected is None:
            return None
        p = self.path(entry[which + "_file"])
        if digest(p) != expected:
            raise TransactionError(f"повреждена сохранённая версия {rel} ({which})")
        return p.read_bytes()

    def snapshot_document(self, op_id, rel, which="before"):
        raw = self._snapshot(self.manifest(op_id), rel, which)
        return None if raw is None else json.loads(raw.decode("utf-8"))

    def _validate_moves(self, moves):
        sources, destinations = set(), set()
        prepared = []
        for source, destination in moves:
            src, dst = self.path(source), self.path(destination)
            if src in sources or dst in destinations or src == dst:
                raise TransactionError("повторяющийся путь в файловой операции")
            sources.add(src); destinations.add(dst)
            if not src.is_file() or dst.exists():
                raise TransactionError(f"нет исходника или занято назначение: {src} -> {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.stat().st_dev != dst.parent.stat().st_dev:
                raise TransactionError("перемещение между разными дисками запрещено")
            prepared.append(dict(source=self.relative(src), target=self.relative(dst),
                                 sha=digest(src), size=src.stat().st_size))
        if sources & destinations:
            raise TransactionError("циклическое перемещение файлов запрещено")
        return prepared

    def commit(self, documents: dict[str, bytes | None], moves=(), *, expected=None,
               label="изменение", uids=(), undoable=False, undo_of=None, op_id=None,
               precondition=None):
        """Move media and atomically replace documents, with durable rollback.

        expected maps document paths to their last observed SHA256, including None
        for a missing file. A mismatch refuses to overwrite another writer.
        """
        op_id = op_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", op_id):
            raise TransactionError("недопустимый идентификатор операции")
        with directory_lock(self.directory):
            if precondition is not None:
                precondition()
            # Include guards for unchanged documents too. Preparation and
            # Studio share this lock; checking before the lock leaves a race.
            for rel, wanted in (expected or {}).items():
                if digest(self.path(rel)) != wanted:
                    raise TransactionError(f"{rel} изменён другим процессом; запись отменена")
            folder = self.directory / op_id
            folder.mkdir(parents=False, exist_ok=False)
            record = dict(schema=1, id=op_id, at=time.time(), label=label, uids=list(uids),
                          undoable=bool(undoable), undo_of=undo_of, state="preparing", moves=[], documents=[])
            # No book data has changed at this stage. A half-created staging
            # directory is harmless and contains no committed operation manifest.
            record["moves"] = self._validate_moves(moves)
            for index, (rel, after) in enumerate(documents.items()):
                p = self.path(rel)
                rel = self.relative(p)
                before = p.read_bytes() if p.exists() else None
                bsha = hashlib.sha256(before).hexdigest() if before is not None else None
                if expected is not None and rel in expected and expected[rel] != bsha:
                    raise TransactionError(f"{rel} изменён другим процессом; запись отменена")
                entry = dict(path=rel, before_sha=bsha,
                             after_sha=hashlib.sha256(after).hexdigest() if after is not None else None)
                for name, content in (("before", before), ("after", after)):
                    snap = folder / f"{index:03d}_{name}.data"
                    entry[name + "_file"] = self.relative(snap)
                    if content is not None:
                        atomic_bytes(snap, content)
                record["documents"].append(entry)
            record["state"] = "prepared"
            self._record(record)
            try:
                for m in record["moves"]:
                    src, dst = self.path(m["source"]), self.path(m["target"])
                    if dst.exists() or digest(src) != m["sha"]:
                        raise TransactionError(f"файл изменён во время операции: {src}")
                    os.rename(src, dst)
                    _sync_dir(src.parent); _sync_dir(dst.parent)
                    if digest(dst) != m["sha"]:
                        raise TransactionError(f"контрольная сумма после перемещения не совпала: {dst}")
                for d in record["documents"]:
                    target = self.path(d["path"])
                    if digest(target) != d["before_sha"]:
                        raise TransactionError(f"документ изменён во время операции: {target}")
                    data = self._snapshot(record, d["path"], "after")
                    if data is None:
                        if target.exists():
                            target.unlink(); _sync_dir(target.parent)
                    else:
                        atomic_bytes(target, data)
                record["state"] = "committed"
                self._record(record)
                return record
            except Exception as exc:
                try:
                    self._rollback(record)
                except Exception as rollback:
                    record["state"] = "recovery_required"
                    record["error"] = str(exc)
                    record["recovery_error"] = str(rollback)
                    try:
                        self._record(record)
                    except OSError:
                        pass
                    raise TransactionError(f"операция прервана; восстановление требует проверки: {rollback}") from exc
                raise TransactionError(f"операция отменена, прежние файлы восстановлены: {exc}") from exc

    def _rollback(self, record):
        # Prevalidate the complete rollback. Never overwrite an unrelated file.
        for d in record["documents"]:
            if digest(self.path(d["path"])) not in (d["before_sha"], d["after_sha"]):
                raise TransactionError(f"документ изменён вне операции: {d['path']}")
            self._snapshot(record, d["path"], "before")
        for m in record["moves"]:
            src, dst = self.path(m["source"]), self.path(m["target"])
            if src.exists() and dst.exists():
                raise TransactionError(f"оба пути заняты: {m['source']}")
            actual = src if src.exists() else dst
            if digest(actual) != m["sha"]:
                raise TransactionError(f"нет проверенной копии: {m['source']}")
        # Move first, including a just-renamed file whose post-rename hash raised.
        # Outcomes are inferred from paths, not an incomplete in-memory list.
        for m in reversed(record["moves"]):
            src, dst = self.path(m["source"]), self.path(m["target"])
            if not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                os.rename(dst, src)
                _sync_dir(src.parent); _sync_dir(dst.parent)
        for d in reversed(record["documents"]):
            target = self.path(d["path"])
            content = self._snapshot(record, d["path"], "before")
            if content is None:
                if target.exists():
                    target.unlink(); _sync_dir(target.parent)
            else:
                atomic_bytes(target, content)
        record["state"] = "rolled_back"
        self._record(record)

    def recover(self):
        """Recover interrupted operations only. Committed history is retained."""
        recovered = []
        with directory_lock(self.directory):
            for folder in sorted(self.directory.iterdir()):
                if not folder.is_dir() or not (folder / "operation.json").exists():
                    continue
                record = self.manifest(folder.name)
                if record["state"] in ("prepared", "recovery_required"):
                    self._rollback(record)
                    recovered.append(record["id"])
                elif record["state"] not in ("committed", "rolled_back"):
                    raise TransactionError(f"неизвестное состояние операции {folder.name}")
        return recovered

    def history(self):
        records = []
        if self.directory.is_dir():
            for p in self.directory.glob("*/operation.json"):
                records.append(self.manifest(p.parent.name))
        return sorted(records, key=lambda x: x["at"])

    def pending_undo(self):
        history = self.history()
        undone = {x.get("undo_of") for x in history if x["state"] == "committed" and x.get("undo_of")}
        return [x for x in history if x["state"] == "committed" and x.get("undoable") and x["id"] not in undone]

    def reverse_moves(self, op_id):
        record = self.manifest(op_id)
        if record["state"] != "committed":
            raise TransactionError("эта операция не завершена")
        moves = []
        for m in reversed(record["moves"]):
            original = digest(self.path(m["source"]))
            archived = digest(self.path(m["target"]))
            if original is not None:
                if original != m["sha"]:
                    raise TransactionError(f"место возврата занято другой записью: {m['source']}")
                # Loading a project can already have copied this exact file
                # back. Undo restores only the remaining members of the bundle.
                continue
            if archived != m["sha"]:
                raise TransactionError(f"файл корзины отсутствует или изменён: {m['target']}")
            moves.append((m["target"], m["source"]))
        return moves
