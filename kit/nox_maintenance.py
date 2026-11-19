"""Recoverable NOX book maintenance. Standard library; Python 3.10+.

The default command is a read-only plan. --apply quarantines complete files on
the same volume, with a durable SHA256 manifest and a reversible transaction.
Neither mode deletes audio or terminates processes. --no-backup omits only an
additional snapshot of retained input metadata; quarantine is always retained.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import uuid

from nox_file_transactions import (FileTransactions, TransactionError,
                                   atomic_bytes, atomic_json, digest, directory_lock)
from nox_freeze import ProtectionError, _csv_rows, read_protection
from nox_update import process_uses_path


class MaintenanceError(RuntimeError):
    pass


VERSION = "2026-11-18d"
MODES = ("pipeline", "edition")
WORK_KEEP = frozenset((
    "voices", "voices_old", "roles.json", "settings.json", "accent_dict.csv",
    "speaker_overrides.tsv", "stress_words.txt", "stress_fixes.tsv",
    "voicecheck_tries.json", "voicecheck.json", "voicecheck.txt", "role_map.tsv",
    "freeze_backups", "audio_history", "retained_audio", "voice_history", "input_history", "nox_release.json",
))
PIPELINE_KEEP = frozenset(("segments.csv", "parts.json", "assembly.json"))
STUDIO_KEEP = frozenset(("sounds", "imports", "home.id", "sound_roots.json", "edge-profile"))
ROOT_PATTERNS = (
    "parts", "parts_zvuk", "sample", "probe", "__pycache__", "book.mp3", "book.m4b",
    "book.*.tmp*", "*.wav.tmp", "fast_w*.log", "log_fast.txt", "verify.log", "verify.err.log",
    "zvuk.log", "zvuk.err.log", "part.log", "part.err.log", "studio_go.log", "studio_engine.log",
)
GUARD_REQUIRED = ("work/segments.csv", "studio/edits.json", "studio/fixed_parts.json", "book.docx")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise MaintenanceError(f"не читается {path}: {exc}") from exc


def _link(path):
    st = path.lstat()
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def book_path(value):
    p = Path(value).absolute()
    if not p.is_dir() or _link(p) or p.parent == p:
        raise MaintenanceError("нужна существующая обычная папка книги, не ссылка и не корень диска")
    p = p.resolve()
    if not (p / "work").is_dir():
        raise MaintenanceError(f"папка не похожа на проект NOX: нет {p / 'work'}")
    safe(p, "work")
    return p


def safe(root, relative):
    """Confine every source, destination and ancestor, including NTFS junctions."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise MaintenanceError(f"небезопасный относительный путь: {relative!r}")
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in (".", "..") for p in relative.split("/")):
        raise MaintenanceError(f"небезопасный относительный путь: {relative!r}")
    current = Path(root)
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _link(current):
                raise MaintenanceError(f"ссылка или junction в пути: {current}")
    resolved = current.resolve()
    if Path(root) not in resolved.parents:
        raise MaintenanceError(f"путь вне книги: {relative}")
    return current


def _files(root, rel):
    p = safe(root, rel)
    if not p.exists():
        return
    if p.is_file():
        yield rel
    elif p.is_dir():
        for child in sorted(p.iterdir(), key=lambda x: x.name.casefold()):
            yield from _files(root, (PurePosixPath(rel) / child.name).as_posix())
    else:
        raise MaintenanceError(f"необычный тип файла: {p}")


def enumerate_processes():
    """Read-only inventory. A failed OS query never means 'no processes'."""
    if os.name == "nt":
        code = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding="
                "New-Object System.Text.UTF8Encoding $false; "
                "@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine) "
                "| ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                               capture_output=True, check=True, timeout=30)
            result = json.loads(r.stdout.decode("utf-8-sig"))
            result = [result] if isinstance(result, dict) else result
            if not isinstance(result, list):
                raise ValueError("нет списка процессов")
            return result
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
            raise MaintenanceError("не удалось проверить процессы Windows; очистка запрещена") from exc
    proc = Path("/proc")
    if not proc.is_dir():
        raise MaintenanceError("эта ОС не поддерживает проверку процессов; очистка запрещена")
    result = []
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            result.append({"ProcessId": int(p.name), "Name": (p / "comm").read_text().strip(),
                           "CommandLine": (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"),
                           "cwd": str((p / "cwd").resolve())})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # PID existence still protects a book's own PID records.
            result.append({"ProcessId": int(p.name), "Name": "unreadable", "CommandLine": ""})
    return result


def check_processes(root, process_reader=enumerate_processes):
    processes = process_reader()
    if not isinstance(processes, list):
        raise MaintenanceError("проверка процессов вернула неверный результат")
    ignored = {os.getpid(), os.getppid()}
    pids, busy = set(), []
    for p in processes:
        try:
            pid = int(p["ProcessId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MaintenanceError("повреждён результат проверки процессов") from exc
        if pid in ignored:
            continue
        pids.add(pid)
        name = str(p.get("Name") or "").casefold()
        command = str(p.get("CommandLine") or "")
        writer = any(x in name for x in ("python", "powershell", "pwsh", "ffmpeg", "cmd.exe"))
        rooted = process_uses_path(command, root) or p.get("cwd") == str(root)
        if writer and rooted:
            busy.append({"pid": pid, "name": name})
    lock = safe(root, "studio/studio.lock")
    if lock.exists():
        doc = _read(lock)
        if not isinstance(doc, dict) or not isinstance(doc.get("pid"), int):
            raise MaintenanceError("studio.lock повреждён; нельзя подтвердить остановку студии")
        if doc["pid"] in pids:
            busy.append({"pid": doc["pid"], "name": "Studio (studio.lock)"})
    queue = safe(root, "work/studio_queue")
    if queue.is_dir():
        for f in queue.glob("alive_w*.txt"):
            safe(root, f.relative_to(root).as_posix())
            try:
                pid = int(f.read_text(encoding="utf-8").split("\t")[0])
            except (OSError, UnicodeError, ValueError) as exc:
                raise MaintenanceError(f"не читается метка воркера {f.name}") from exc
            if pid in pids:
                busy.append({"pid": pid, "name": f.name})
    # verify's old heartbeat has no PID. A fresh signal is intentionally enough
    # to refuse: its relative command line need not contain the absolute root.
    heart = safe(root, "work/asr_active.txt")
    if heart.exists() and time.time() - heart.stat().st_mtime < 125:
        busy.append({"pid": None, "name": "свежая метка проверки asr_active.txt"})
    if busy:
        detail = ", ".join(f"{p['name']} (PID {p['pid']})" if p["pid"] else p["name"] for p in busy)
        raise MaintenanceError("книга используется: " + detail + ". Завершите работу штатно; процессы не остановлены")


def _strict_state(root, *, allow_own_marker=False):
    for rel in GUARD_REQUIRED[:3]:
        safe(root, rel)
    csv_path = root / "work/segments.csv"
    rows = _csv_rows(csv_path.read_bytes() if csv_path.exists() else None)
    state = read_protection(root, rows, allow_preparing=allow_own_marker)
    if state.ranges or state.locked_uids or state.protected_indices or state.opened_ranges:
        raise MaintenanceError("очистка запрещена: есть закреплённые фразы/части или открытые части Studio; "
                               "ни -NoBackup, ни --apply эту защиту не обходят")
    return state


def _settled_transactions(root):
    from nox_audio_archive import pending_archives
    safe(root, "work/audio_history")
    try:
        pending = pending_archives(root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise MaintenanceError("не читается история перемещений WAV; очистка запрещена") from exc
    if pending:
        raise MaintenanceError("незавершённое архивирование WAV: " + str(pending[0]) + "; сначала восстановите эту операцию")
    for rel, pattern in (("studio/transactions", "*/operation.json"), ("studio/export_transactions", "*.json")):
        folder = safe(root, rel)
        if folder.is_dir():
            for path in folder.glob(pattern):
                safe(root, path.relative_to(root).as_posix())
                doc = _read(path)
                if not isinstance(doc, dict) or doc.get("state") not in ("committed", "rolled_back"):
                    raise MaintenanceError(f"незавершённая или повреждённая операция Studio: {path}; сначала её восстановите")


def _tx(root):
    tx = FileTransactions(root)
    tx.directory = safe(root, "maintenance/transactions")
    return tx


def _pending(root):
    folder = safe(root, "maintenance/quarantine")
    pending = []
    for child in sorted(folder.iterdir()) if folder.is_dir() else ():
        safe(root, child.relative_to(root).as_posix())
        if not child.is_dir() or not _ID.fullmatch(child.name):
            raise MaintenanceError(f"неизвестная папка обслуживания: {child}")
        path = child / "manifest.json"
        if not path.exists():
            # A failed optional staging write can leave an empty directory or
            # temporary metadata. Original files never move before a manifest
            # AND transaction journal are durable. Retain this staging as-is.
            saved = safe(root, child.relative_to(root).as_posix() + "/files")
            journal = safe(root, f"maintenance/transactions/{child.name}/operation.json")
            if journal.exists() or (saved.exists() and any(saved.rglob("*"))):
                raise MaintenanceError(f"нет манифеста {path}; требуется проверка сохранённой папки")
            continue
        manifest = _manifest(root, child.name)
        if manifest["state"] in ("prepared", "restoring"):
            pending.append(manifest)
    transactions = safe(root, "maintenance/transactions")
    if transactions.is_dir():
        for path in transactions.glob("*/operation.json"):
            safe(root, path.relative_to(root).as_posix())
            record = _tx(root).manifest(path.parent.name)
            if record.get("state") in ("prepared", "recovery_required"):
                pending.append({"transaction": record["id"]})
            elif record.get("state") not in ("committed", "rolled_back"):
                raise MaintenanceError(f"неизвестное состояние журнала {path}")
    return pending


def _inputs(root):
    result = set(GUARD_REQUIRED)
    for rel in ("work", "studio"):
        folder = safe(root, rel)
        if folder.is_dir():
            for p in folder.iterdir():
                if p.suffix.casefold() in (".json", ".csv", ".tsv", ".sig", ".ver", ".txt") and p.is_file():
                    if p.name not in ("asr_active.txt", "nox_release.json"):
                        result.add(p.relative_to(root).as_posix())
    for p in root.glob("*.xlsx"):
        if p.is_file():
            result.add(p.name)
    return sorted(result)


def _targets(root, mode):
    result = set()
    keep = WORK_KEEP | (PIPELINE_KEEP if mode == "pipeline" else frozenset())
    for p in sorted((root / "work").iterdir()):
        if p.name not in keep and p.name != ".nox_prepare.lock":
            result.update(_files(root, "work/" + p.name))
    for p in sorted(root.iterdir()):
        if any(fnmatch.fnmatchcase(p.name.casefold(), pat.casefold()) for pat in ROOT_PATTERNS) or (
                mode == "edition" and p.name in ("gotovo", "Разметка_книги.txt")):
            result.update(_files(root, p.name))
    studio = safe(root, "studio")
    if mode == "edition" and studio.is_dir():
        for p in sorted(studio.iterdir()):
            if p.name not in STUDIO_KEEP:
                result.update(_files(root, "studio/" + p.name))
        # This open, process-level lock must stay at its canonical path on
        # Windows. Old Undo journals and their files move together, not this lock.
        result.discard("studio/transactions/operations.lock")
    return sorted(result)


def build_plan(root, mode="pipeline", *, no_backup=False, process_reader=enumerate_processes, progress=None):
    root = book_path(root)
    if mode not in MODES:
        raise MaintenanceError("неизвестный режим обслуживания")
    check_processes(root, process_reader)
    _strict_state(root)
    _settled_transactions(root)
    if _pending(root):
        raise MaintenanceError("есть незавершённая очистка; сначала выполните --recover")
    paths = _targets(root, mode)
    files = []
    for i, rel in enumerate(paths):
        path = safe(root, rel)
        files.append({"source": rel, "sha256": digest(path), "size": path.stat().st_size})
        if progress and (i + 1) % 256 == 0:
            progress(f"Проверено файлов: {i + 1} из {len(paths)}")
    guards = {rel: digest(safe(root, rel)) for rel in _inputs(root)}
    # No audio duplication. Every moved input is already part of quarantine.
    snapshots = [] if no_backup else [dict(path=rel, sha256=sha, size=safe(root, rel).stat().st_size)
                                      for rel, sha in guards.items() if sha and rel not in paths]
    return {"schema": 1, "tool": "NOX-maintenance", "version": VERSION, "root": str(root), "mode": mode,
            "no_backup": bool(no_backup), "files": files, "guards": guards, "snapshots": snapshots,
            "bytes": sum(f["size"] for f in files), "snapshot_bytes": sum(f["size"] for f in snapshots),
            "preserves": ["рукопись, настройки, голоса, окружения, модели, установленный код",
                          "истории freeze_backups, audio_history, retained_audio, voice_history, input_history",
                          "Яндекс Диск, Downloads и другие книги",
                          "вся Studio, готовые части и CSV/границы" if mode == "pipeline" else
                          "библиотека Studio sounds/imports, home.id и внешние каталоги"]}


def _guard_hashes(root, guards, *, ignore=()):
    for rel, expected in guards.items():
        if rel not in ignore and digest(safe(root, rel)) != expected:
            raise MaintenanceError(f"файл изменился после плана: {rel}; ничего не перезаписывается")


def _manifest(root, op_id):
    if not _ID.fullmatch(str(op_id)):
        raise MaintenanceError("идентификатор очистки должен быть 32 шестнадцатеричными символами")
    path = safe(root, f"maintenance/quarantine/{op_id}/manifest.json")
    doc = _read(path)
    if (not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("tool") != "NOX-maintenance"
            or doc.get("id") != op_id or doc.get("root") != str(root) or doc.get("mode") not in MODES
            or doc.get("state") not in ("prepared", "quarantined", "restoring", "restored", "rolled_back")
            or not isinstance(doc.get("files"), list) or not isinstance(doc.get("guards"), dict)):
        raise MaintenanceError(f"повреждён или относится к другой книге манифест {path}")
    seen = set()
    for f in doc["files"]:
        if (not isinstance(f, dict) or not isinstance(f.get("source"), str) or f["source"] in seen
                or not _SHA.fullmatch(str(f.get("sha256"))) or not isinstance(f.get("size"), int)
                or isinstance(f["size"], bool) or f["size"] < 0):
            raise MaintenanceError(f"повреждён список файлов в {path}")
        safe(root, f["source"])
        if f["source"].split("/", 1)[0] in ("maintenance", "env", "envfast", "hf"):
            raise MaintenanceError("манифест пытается восстановить посторонний каталог")
        expected = f"maintenance/quarantine/{op_id}/files/{f['source']}"
        if f.get("target") != expected:
            raise MaintenanceError("неверный путь карантина в манифесте")
        safe(root, expected)
        seen.add(f["source"])
    for rel, sha in doc["guards"].items():
        safe(root, rel)
        if sha is not None and not _SHA.fullmatch(str(sha)):
            raise MaintenanceError("повреждена контрольная сумма метаданных")
    return doc


def _record_matches(doc, record, *, reverse=False):
    if (not isinstance(record.get("moves"), list) or not all(isinstance(m, dict) for m in record["moves"])
            or not isinstance(record.get("documents"), list)
            or len(record["documents"]) != 1 or not isinstance(record["documents"][0], dict)):
        raise MaintenanceError("повреждена структура журнала обслуживания")
    want = [(f["target"], f["source"], f["sha256"], f["size"]) if reverse else
            (f["source"], f["target"], f["sha256"], f["size"]) for f in doc["files"]]
    actual = [(m.get("source"), m.get("target"), m.get("sha"), m.get("size")) for m in record.get("moves", [])]
    if sorted(want) != sorted(actual):
        raise MaintenanceError("SHA-манифест не соответствует журналу перемещения")
    manifest_rel = f"maintenance/quarantine/{doc['id']}/manifest.json"
    if len(record.get("documents", [])) != 1 or record["documents"][0].get("path") != manifest_rel:
        raise MaintenanceError("журнал содержит посторонние изменения документов")


@contextlib.contextmanager
def _exclusive(root, marker_data, process_reader):
    """Share the Studio/preparation lock and a marker understood by workers."""
    safe(root, "studio/transactions")
    with directory_lock(root / "studio/transactions"):
        check_processes(root, process_reader)
        marker = safe(root, "work/.nox_prepare.lock")
        if marker.exists():
            raise MaintenanceError("другая подготовка или очистка уже начата; при прерывании используйте --recover")
        data = dict(marker_data, purpose="maintenance", root=str(root), pid=os.getpid(), token=uuid.uuid4().hex, at=time.time())
        with marker.open("xb") as f:
            f.write(_json(data)); f.flush(); os.fsync(f.fileno())
        try:
            yield data
        except BaseException:
            # Caller explicitly removes the marker only if rollback is proved.
            raise


def _remove_marker(root, data):
    marker = safe(root, "work/.nox_prepare.lock")
    if not marker.exists() or _read(marker) != data:
        raise MaintenanceError("служебная блокировка изменилась; она оставлена для проверки")
    marker.unlink()


def _validate_fresh_plan(root, plan, process_reader):
    _strict_state(root, allow_own_marker=True)
    _settled_transactions(root)
    check_processes(root, process_reader)
    _guard_hashes(root, plan["guards"])
    if _targets(root, plan["mode"]) != [f["source"] for f in plan["files"]]:
        raise MaintenanceError("состав файлов изменился после плана; требуется новый план")
    for f in plan["files"]:
        p = safe(root, f["source"])
        if not p.is_file() or p.stat().st_size != f["size"] or digest(p) != f["sha256"]:
            raise MaintenanceError(f"файл изменился после плана: {f['source']}")


def apply_plan(root, plan, *, process_reader=enumerate_processes):
    root = book_path(root)
    # A caller cannot turn a fabricated plan into an arbitrary file mover.
    current = build_plan(root, plan.get("mode"), no_backup=plan.get("no_backup", False), process_reader=process_reader)
    if current != plan:
        raise MaintenanceError("план устарел или изменён; сначала получите новый план")
    if not plan["files"]:
        return {"state": "nothing_to_move", "files": 0, "bytes": 0}
    op_id = uuid.uuid4().hex
    rel = f"maintenance/quarantine/{op_id}/manifest.json"
    manifest = dict(plan, id=op_id, at=time.time(), state="prepared")
    manifest["files"] = [dict(f, target=f"maintenance/quarantine/{op_id}/files/{f['source']}") for f in plan["files"]]
    tx = _tx(root)
    safe(root, rel)
    with _exclusive(root, {"id": op_id, "transaction": op_id, "action": "apply"}, process_reader) as marker:
        try:
            _validate_fresh_plan(root, plan, process_reader)
            atomic_json(safe(root, rel), manifest)
            for snapshot in plan["snapshots"]:
                p = safe(root, snapshot["path"])
                target = safe(root, f"maintenance/quarantine/{op_id}/inputs/{snapshot['path']}")
                data = p.read_bytes()
                atomic_bytes(target, data)
                if digest(p) != snapshot["sha256"] or digest(target) != snapshot["sha256"]:
                    raise MaintenanceError(f"изменился входной файл при копировании: {snapshot['path']}")
            after = dict(manifest, state="quarantined")
            tx.commit({rel: _json(after)}, [(f["source"], f["target"]) for f in manifest["files"]],
                      expected=dict(plan["guards"], **{rel: digest(safe(root, rel))}),
                      label="NOX maintenance " + plan["mode"], op_id=op_id,
                      precondition=lambda: _validate_fresh_plan(root, plan, process_reader))
        except Exception:
            record_path = tx.directory / op_id / "operation.json"
            record = tx.manifest(op_id) if record_path.exists() else None
            if record is None or record.get("state") == "rolled_back":
                # With no prepared journal there has been no source mutation.
                if safe(root, rel).exists():
                    atomic_json(safe(root, rel), dict(manifest, state="rolled_back"))
                _remove_marker(root, marker)
            raise
        _remove_marker(root, marker)
    return {"state": "quarantined", "id": op_id, "manifest": str(root / rel),
            "files": len(manifest["files"]), "bytes": plan["bytes"], "freed_bytes": 0,
            "snapshot_bytes": plan["snapshot_bytes"]}


def restore_plan(root, op_id, *, process_reader=enumerate_processes):
    root = book_path(root)
    check_processes(root, process_reader)
    _strict_state(root)
    _settled_transactions(root)
    if _pending(root):
        raise MaintenanceError("есть незавершённая операция; сначала --recover")
    doc = _manifest(root, op_id)
    record = _tx(root).manifest(op_id)
    _record_matches(doc, record)
    if record.get("state") != "committed" or doc["state"] not in ("quarantined", "restored"):
        raise MaintenanceError("эта очистка не завершена или уже отменена")
    moved = {f["source"] for f in doc["files"]}
    _guard_hashes(root, doc["guards"], ignore=moved)
    for f in doc["files"]:
        src, dst = safe(root, f["source"]), safe(root, f["target"])
        expected_path = src if doc["state"] == "restored" else dst
        absent_path = dst if doc["state"] == "restored" else src
        if absent_path.exists() or digest(expected_path) != f["sha256"]:
            raise MaintenanceError(f"нет целой проверенной записи или занято место восстановления: {f['source']}")
    return doc


def restore(root, op_id, *, process_reader=enumerate_processes):
    root = book_path(root)
    doc = restore_plan(root, op_id, process_reader=process_reader)
    if doc["state"] == "restored":
        return {"state": "already_restored", "id": op_id}
    tx = _tx(root)
    restore_id = uuid.uuid4().hex
    rel = f"maintenance/quarantine/{op_id}/manifest.json"
    original_sha = digest(root / rel)
    with _exclusive(root, {"id": op_id, "transaction": restore_id, "action": "restore"}, process_reader) as marker:
        try:
            def recheck():
                _strict_state(root, allow_own_marker=True)
                check_processes(root, process_reader)
                _guard_hashes(root, doc["guards"], ignore={f["source"] for f in doc["files"]})
                for f in doc["files"]:
                    if safe(root, f["source"]).exists() or digest(safe(root, f["target"])) != f["sha256"]:
                        raise MaintenanceError(f"восстановление конфликтует с файлом {f['source']}")
            recheck()
            after = dict(doc, state="restored", restored_by=restore_id, restored_at=time.time())
            tx.commit({rel: _json(after)}, [(f["target"], f["source"]) for f in reversed(doc["files"])],
                      expected={rel: original_sha}, op_id=restore_id, label="NOX maintenance restore",
                      precondition=recheck)
        except Exception:
            path = tx.directory / restore_id / "operation.json"
            record = tx.manifest(restore_id) if path.exists() else None
            if record is None or record.get("state") == "rolled_back":
                _remove_marker(root, marker)
            raise
        _remove_marker(root, marker)
    return {"state": "restored", "id": op_id, "transaction": restore_id, "files": len(doc["files"])}


def recovery_plan(root, *, process_reader=enumerate_processes):
    root = book_path(root)
    check_processes(root, process_reader)
    marker_path = safe(root, "work/.nox_prepare.lock")
    if not marker_path.exists():
        if _pending(root):
            raise MaintenanceError("незавершённый манифест без своей блокировки; автоматическое восстановление запрещено")
        return {"state": "nothing_to_recover"}
    marker = _read(marker_path)
    if (not isinstance(marker, dict) or marker.get("purpose") != "maintenance"
            or marker.get("root") != str(root)
            or not _ID.fullmatch(str(marker.get("id"))) or not _ID.fullmatch(str(marker.get("transaction")))
            or marker.get("action") not in ("apply", "restore")):
        raise MaintenanceError("блокировка принадлежит другой подготовке; инструмент очистки её не снимает")
    tx = _tx(root)
    path = tx.directory / marker["transaction"] / "operation.json"
    record = tx.manifest(marker["transaction"]) if path.exists() else None
    reverse = marker["action"] == "restore"
    manifest_path = safe(root, f"maintenance/quarantine/{marker['id']}/manifest.json")
    if not manifest_path.exists():
        # The marker precedes the manifest. No rename is allowed until the
        # durable file transaction exists; do not strand this crash window.
        quarantine_files = safe(root, f"maintenance/quarantine/{marker['id']}/files")
        if reverse or record is not None or (quarantine_files.exists() and any(quarantine_files.rglob("*"))):
            raise MaintenanceError("нет манифеста начатого перемещения; требуется проверка файлов")
        return {"state": "recovery_available", "marker": marker, "manifest": None, "record": None}
    doc = _manifest(root, marker["id"])
    moved = {f["source"] for f in doc["files"]}
    _guard_hashes(root, doc["guards"], ignore=moved)
    if record is not None:
        _record_matches(doc, record, reverse=reverse)
        if record.get("state") not in ("prepared", "recovery_required", "committed", "rolled_back"):
            raise MaintenanceError("неизвестное состояние журнала обслуживания")
    else:
        # A crash while copying the optional metadata snapshot precedes the
        # durable transaction. Prove that every original is still untouched.
        for f in doc["files"]:
            original = safe(root, f["target"] if reverse else f["source"])
            absent = safe(root, f["source"] if reverse else f["target"])
            if absent.exists() or digest(original) != f["sha256"]:
                raise MaintenanceError("без журнала нельзя доказать целостность всех исходных файлов")
    return {"state": "recovery_available", "marker": marker, "manifest": doc, "record": record}


def recover(root, *, process_reader=enumerate_processes):
    root = book_path(root)
    plan = recovery_plan(root, process_reader=process_reader)
    if plan["state"] == "nothing_to_recover":
        return plan
    marker = plan["marker"]
    with directory_lock(safe(root, "studio/transactions")):
        check_processes(root, process_reader)
        fresh = recovery_plan(root, process_reader=process_reader)
        if fresh != plan:
            raise MaintenanceError("состояние восстановления изменилось; повторите проверку")
        record, doc = plan["record"], plan["manifest"]
        tx = _tx(root)
        if record is not None:
            with directory_lock(tx.directory):
                if record["state"] in ("prepared", "recovery_required"):
                    # The shared implementation prevalidates ALL moves and
                    # document snapshots before the first reverse rename.
                    tx._rollback(record)
                elif record["state"] == "committed":
                    for f in doc["files"]:
                        done = f["source"] if marker["action"] == "restore" else f["target"]
                        absent = f["target"] if marker["action"] == "restore" else f["source"]
                        if digest(safe(root, done)) != f["sha256"] or safe(root, absent).exists():
                            raise MaintenanceError("завершённая операция повреждена; блокировка сохранена")
                    for d in record["documents"]:
                        if digest(safe(root, d["path"])) != d["after_sha"]:
                            raise MaintenanceError("манифест завершённой операции изменён")
                if record["state"] == "rolled_back":
                    for item in record["moves"]:
                        if (digest(safe(root, item["source"])) != item["sha"]
                                or safe(root, item["target"]).exists()):
                            raise MaintenanceError("откат не подтверждён целым комплектом файлов; блокировка сохранена")
        if doc is not None and (record is None or record["state"] == "rolled_back") and marker["action"] == "apply":
            atomic_json(safe(root, f"maintenance/quarantine/{doc['id']}/manifest.json"), dict(doc, state="rolled_back"))
        _strict_state(root, allow_own_marker=True)
        _remove_marker(root, marker)
    return {"state": "recovered", "id": marker["id"], "action": marker["action"],
            "outcome": record["state"] if record else "no_source_changes"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=r"D:\kniga1a")
    parser.add_argument("--mode", choices=MODES, default="pipeline")
    parser.add_argument("--no-backup", action="store_true", help="omit only the extra snapshot of retained input metadata")
    parser.add_argument("--apply", action="store_true", help="perform the displayed recoverable operation")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--restore", metavar="ID")
    action.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = book_path(args.root)
        print(f"NOX {VERSION}: {root}\nПроверка защит, процессов и контрольных сумм…", flush=True)
        if args.recover:
            plan = recovery_plan(root)
            print("Восстановление прерванной операции: " + plan["state"])
            result = recover(root) if args.apply else {"state": "plan_only"}
        elif args.restore:
            plan = restore_plan(root, args.restore)
            print(f"Возврат полного комплекта: {len(plan['files'])} файлов; операция {args.restore}")
            result = restore(root, args.restore) if args.apply else {"state": "plan_only"}
        else:
            plan = build_plan(root, args.mode, no_backup=args.no_backup, progress=lambda s: print(s, flush=True))
            print(f"Режим: {args.mode}. В карантин: {len(plan['files'])} файлов, {plan['bytes'] / 1024**3:.3f} ГБ.")
            print(f"Дополнительная копия входных метаданных: {plan['snapshot_bytes'] / 1024**2:.2f} МБ.")
            for line in plan["preserves"]:
                print("Сохраняются: " + line)
            print("Перемещение сохраняет файлы на этом же диске и НЕ освобождает занятое ими место.")
            groups = {}
            for f in plan["files"]:
                key = "/".join(f["source"].split("/")[:2])
                groups[key] = groups.get(key, 0) + 1
            for group, count in groups.items():
                print(f"  {group}: {count}")
            result = apply_plan(root, plan) if args.apply else {"state": "plan_only"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.apply:
            print("Это только план. Для выполнения повторите команду с -Apply (PowerShell) или --apply (Python).")
        elif result.get("state") == "quarantined":
            print(f"Для возврата: --root \"{root}\" --restore {result['id']} --apply")
        return 0
    except (MaintenanceError, ProtectionError, TransactionError, OSError, ValueError) as exc:
        print(f"СТОП: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
