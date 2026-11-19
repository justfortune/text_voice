"""Integrated 18d backend, extending the installed 12b Studio contracts.

The original renderer, editor, import/export and pipeline methods remain the
base implementation. The subclass below replaces the unsafe mutation paths and
adds the recovered late features. It is installed once before the HTTP server
is defined, not monkey-patched at request time.
"""
from __future__ import annotations

import collections
import copy
import csv
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import unicodedata
import uuid
import zipfile

from nox_file_transactions import FileTransactions, TransactionError, atomic_bytes, atomic_json, digest, directory_lock
from nox_sound_catalog import ENT_RU, ACT_RU, sfx_describe, bounded_scan

BUILD = "2026-11-19-recovery2-public1"


def trim_spans(value, dur=None, *, strict=False):
    if value is None:
        return []
    try:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("нужно выбрать хотя бы один участок записи")
        raw = value if isinstance(value[0], (list, tuple)) else [value]
        out = []
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("границы участка — пара чисел")
            a, b = map(float, pair)
            if not math.isfinite(a) or not math.isfinite(b) or b <= a:
                raise ValueError("границы участка должны быть конечными числами по возрастанию")
            if dur is not None:
                a, b = max(0., min(a, dur)), max(0., min(b, dur))
            if b - a >= .02:
                out.append([round(a, 3), round(b, 3)])
        out.sort()
        merged = []
        for a, b in out:
            if merged and a - merged[-1][1] < .02:
                merged[-1][1] = max(b, merged[-1][1])
            else:
                merged.append([a, b])
        if strict and not merged:
            raise ValueError("не осталось ни одного участка записи")
        return merged
    except (TypeError, ValueError, OverflowError) as exc:
        if strict:
            raise ValueError(str(exc)) from exc
        return []


def trim_total(value, dur=None):
    return sum(b - a for a, b in trim_spans(value, dur))


def cut_audio(audio, sr, value):
    import numpy as np
    spans = trim_spans(value, len(audio) / sr, strict=True)
    output = None
    fade = max(1, int(.012 * sr))
    for a, b in spans:
        chunk = np.asarray(audio[int(a * sr):int(b * sr)], dtype="float32")
        if output is None:
            output = chunk
        elif len(output) >= fade and len(chunk) >= 2 * fade:
            weight = np.linspace(0., 1., fade, dtype="float32")
            output = np.concatenate((output[:-fade], output[-fade:] * (1. - weight) + chunk[:fade] * weight, chunk[fade:]))
        else:
            output = np.concatenate((output, chunk))
    return output


def validate_store(doc):
    if not isinstance(doc, dict):
        raise ValueError("edits.json должен содержать объект")
    for key, kind in (("segs", dict), ("parts", list), ("rules", list), ("orphans", list), ("ins", list),
                      ("cues", list), ("mix", dict), ("sound_map", dict), ("chapters", dict)):
        if key in doc and not isinstance(doc[key], kind):
            raise ValueError(f"edits.json: неверное поле {key}")
    if "segs" not in doc or "parts" not in doc:
        raise ValueError("edits.json: отсутствуют segs/parts")
    for uid, e in doc["segs"].items():
        if not isinstance(uid, str) or not isinstance(e, dict):
            raise ValueError("edits.json: повреждена запись фразы")
        if e.get("sid") and not re.fullmatch(r"s\d+", str(e["sid"])):
            raise ValueError(f"edits.json: неверный sid у {uid}")
        if e.get("locked") and not isinstance(e["locked"], dict):
            raise ValueError(f"edits.json: повреждён снимок закрепления {uid}")
        if e.get("trims") is not None and not isinstance(e["trims"], dict):
            raise ValueError(f"edits.json: повреждены обрезки {uid}")
        for tid, spans in (e.get("trims") or {}).items():
            if not re.fullmatch(r"t\d+", str(tid)):
                raise ValueError(f"edits.json: неверный номер обрезки {uid}")
            trim_spans(spans, strict=True)
    for i, part in enumerate(doc["parts"], 1):
        if not isinstance(part, dict) or not isinstance(part.get("last"), str) or not part["last"]:
            raise ValueError(f"edits.json: повреждена граница части {i}")
    return doc


def _same_entry(a, b):
    def normalize(value):
        out = copy.deepcopy(value)
        if isinstance(out, dict):
            out.pop("tseq", None)
        return out
    return normalize(a) == normalize(b)


def make_studio_class(Base, g):
    Oops = g["Oops"]

    class Studio(Base):
        release_build = BUILD
        EXT_CAP = 30000

        def mutation_guard(self):
            if (self.root / "work" / ".nox_prepare.lock").exists():
                raise Oops("идёт подготовка книги; дождитесь её завершения перед правкой")
            if (self.root / "work" / ".nox_roles.lock").exists():
                raise Oops("идёт сохранение ролей и прежних голосов; дождитесь завершения перед правкой")
            if getattr(self, "_recovery_blocked", None):
                raise Oops("не завершено восстановление файлов: " + str(self._recovery_blocked))

        def _pipeline_rows(self):
            return [self.rows[i] for i in self.sidx] if hasattr(self, "sidx") else self.rows

        def _derive_part_selected_uids(self):
            """Metadata-only index, refreshed after a validated publication.

            These selections are protected from automatic replacement. They
            remain manually editable and are not per-phrase frozen snapshots.
            """
            from nox_freeze import _insertion_anchors, _part_members, ProtectionError
            # A last-anchored insertion belongs to this part even though it
            # follows the part's last raw UID in Studio's expanded row list.
            # Reuse the exact freeze membership rules, including chained and
            # before-first insertions. All inputs are validated memory state.
            pipeline_uids = [self.uids[i] for i in self.sidx] if hasattr(self, "sidx") else self.uids
            positions = {uid: i for i, uid in enumerate(pipeline_uids)}
            try:
                _insertions, anchors = _insertion_anchors(self.store, positions)
            except ProtectionError as exc:
                raise Oops(str(exc)) from exc
            selected, first = set(), 0
            for part in self.store.get("parts", []):
                last = positions.get(part.get("last"))
                if last is None or last < first:
                    raise Oops("граница закреплённой части не подтверждена; автоматическая правка остановлена")
                if part.get("locked"):
                    for uid in _part_members(pipeline_uids, anchors, first, last):
                        entry = self.store["segs"].get(uid) or {}
                        if not entry.get("locked") and entry.get("sid") and entry.get("take"):
                            selected.add(uid)
                first = last + 1
            return frozenset(selected)

        def _part_selected_guard_uids(self):
            # Keep the previously validated UID when reanchor is examining a
            # changed row layout; never infer its replacement from the new UID.
            previous = getattr(self, "_part_selected", None)
            return previous if previous is not None else self._derive_part_selected_uids()

        def load_store(self):
            self.mutation_guard()
            from nox_release_io import recover_publications, PublicationError
            try:
                with directory_lock(self.dir / "transactions"):
                    self.mutation_guard()
                    recover_publications(self.dir / "export_transactions", self.root)
            except (PublicationError, TransactionError) as exc:
                self._recovery_blocked = str(exc)
                raise Oops("не завершено восстановление экспорта: " + str(exc)) from exc
            self._tx = FileTransactions(self.dir)
            try:
                recovered = self._tx.recover()
            except TransactionError as exc:
                self._recovery_blocked = str(exc)
                raise Oops(str(exc)) from exc
            path = self.dir / "edits.json"
            if path.exists():
                try:
                    data = path.read_bytes()
                    st = validate_store(json.loads(data.decode("utf-8-sig")))
                except (OSError, ValueError, TypeError) as exc:
                    raise Oops(f"edits.json повреждён или не читается; исходник не изменён. "
                               f"Выберите проверенную точку возврата/копию в {self.backup_dir}. Причина: {exc}") from exc
            else:
                # New books may legitimately have no Studio state. A pre-existing
                # media or lock store is not silently converted into an empty book.
                if (self.dir / "fixed_parts.json").exists():
                    try:
                        fixed = json.loads((self.dir / "fixed_parts.json").read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError) as exc:
                        raise Oops("нет edits.json, а fixed_parts.json повреждён; восстановите согласованную копию") from exc
                    if not isinstance(fixed, dict) or not isinstance(fixed.get("parts"), list) or fixed["parts"]:
                        raise Oops("нет edits.json при существующих закреплениях; пустой проект не создан")
                if any(self.takes_dir.glob("*/*.json")) or any(self.takes_dir.glob("*/*.wav")):
                    raise Oops("нет edits.json, но сохранились дубли Studio; сначала восстановите правки из копии")
                st = {"v": 1, "segs": {}, "parts": [], "rules": [], "orphans": [], "seq": 0}
                data = None
            was = str(st.get("studio") or "")
            if was > g["VERSION"]:
                raise Oops(f"правки записаны более новой студией ({was}); запущена {g['VERSION']}")
            self._edits_sha = digest(path)
            self._fixed_sha = digest(self.dir / "fixed_parts.json")
            self._last_durable_store = copy.deepcopy(st)
            from nox_freeze import read_protection, ProtectionError
            try:
                protection = read_protection(self.root, self._pipeline_rows(), check_audio=True)
            except ProtectionError as exc:
                raise Oops("согласованность закреплённой редакции не подтверждена: " + str(exc)) from exc
            self.store = self.store_defaults(st)
            self._part_selected = protection.part_selected_uids
            if path.exists() and was != g["VERSION"]:
                backup = self.backup_dir / f"pered_{g['VERSION']}_{uuid.uuid4().hex[:12]}"
                backup.mkdir(parents=True, exist_ok=False)
                for name in ("edits.json", "fixed_parts.json", "stress.json", "sid_map.json", "home.id"):
                    source = self.dir / name
                    if source.exists():
                        shutil.copy2(source, backup / name)
                        if digest(source) != digest(backup / name):
                            raise Oops("не прошла проверка копии перед обновлением: " + name)
                self._upgraded = was or "прежняя версия"
            self.store["studio"] = g["VERSION"]
            self.store["build"] = BUILD
            if self.store["ins"]:
                self.load_rows(force=True)
            else:
                self.reanchor()
            self._refresh_file_undo()
            for op in recovered:
                g["log"]("восстановлена прерванная файловая операция: " + op, "warn")

        def _documents(self):
            validate_store(self.store)
            from nox_freeze import fixed_document, ProtectionError
            try:
                fixed = fixed_document(self._pipeline_rows(), self.store)
            except ProtectionError as exc:
                raise Oops(str(exc)) from exc
            docs = {"edits.json": json.dumps(self.store, ensure_ascii=False, indent=1).encode("utf-8")}
            current = g["read_json"](self.dir / "fixed_parts.json", None)
            if current is None or current.get("parts") != fixed.get("parts"):
                fixed["at"] = g["now"]()
                docs["fixed_parts.json"] = json.dumps(fixed, ensure_ascii=False, indent=1).encode("utf-8")
            return docs

        def _accept_published_state(self):
            """Called by a successful export publication under the Studio lock."""
            self._edits_sha = digest(self.dir / "edits.json")
            self._fixed_sha = digest(self.dir / "fixed_parts.json")
            self._last_durable_store = copy.deepcopy(self.store)
            self._part_selected = self._derive_part_selected_uids()

        def _commit_state(self, why, rec=None, *, moves=(), uids=(), undoable=False, undo_of=None, op_id=None):
            self.mutation_guard()
            self._tx = getattr(self, "_tx", None) or FileTransactions(self.dir)
            expected = {"edits.json": getattr(self, "_edits_sha", digest(self.dir / "edits.json")),
                        "fixed_parts.json": getattr(self, "_fixed_sha", digest(self.dir / "fixed_parts.json"))}
            try:
                docs = self._documents()
                # Also compare unchanged fixed, otherwise an outside writer could
                # remove protection without being noticed by this save.
                if digest(self.dir / "fixed_parts.json") != expected["fixed_parts.json"]:
                    raise TransactionError("fixed_parts.json изменён другим процессом; обновите окно")
                operation = self._tx.commit(docs, moves, expected=expected, label=why, uids=uids,
                                            undoable=undoable, undo_of=undo_of, op_id=op_id,
                                            precondition=self.mutation_guard)
            except Exception as exc:
                try:
                    self._tx.recover()
                except Exception as recovery:
                    self._recovery_blocked = str(recovery)
                if hasattr(self, "_last_durable_store"):
                    self.store = self.store_defaults(copy.deepcopy(self._last_durable_store))
                self._takes = {}
                if isinstance(exc, Oops):
                    raise
                raise Oops(str(exc)) from exc
            self._edits_sha = digest(self.dir / "edits.json")
            self._fixed_sha = digest(self.dir / "fixed_parts.json")
            self._last_durable_store = copy.deepcopy(self.store)
            self._part_selected = self._derive_part_selected_uids()
            self._takes = {}
            self._oldv = None
            self.journal(dict(rec or {}, why=why, operation=operation["id"]))
            self.nsave = getattr(self, "nsave", 0) + 1
            if self.nsave % 25 == 0:
                self.backup()
            if why not in ("заявка на переозвучку", "получен дубль"):
                g["ev"](self._why_line(why, rec or {}), "edit", "правка")
            if undoable:
                self.undo.append(("#files", operation["id"], why))
                del self.undo[:-80]
            self._prune_document_journals()
            return operation

        def _prune_document_journals(self):
            # A bounded rolling checkpoint for ordinary single-state writes.
            # Media operations and their Undo documents are never purged here.
            try:
                disposable = [x for x in self._tx.history() if x["state"] in ("committed", "rolled_back")
                              and not x.get("moves") and not x.get("undoable") and not x.get("undo_of")]
                for record in disposable[:-20]:
                    shutil.rmtree(self._tx.directory / record["id"])
            except OSError as exc:
                g["log"]("журнал операций сохранён без сокращения: " + str(exc), "warn")

        def save(self, why, rec=None):
            with self.lock:
                return self._commit_state(why, rec)

        def write_fixed(self):
            with self.lock:
                docs = self._documents()
                if "fixed_parts.json" in docs:
                    self._commit_state("согласованы закрепления конвейера")

        def unlock_segs(self, idxs, label):
            """Part and phrase protection change in one consistent save.

            The old implementation saved unlocked phrases while their part
            still declared itself locked. The strict document contract must
            never publish that intermediate state.
            """
            self.mutation_guard()
            with self.lock:
                idxs = sorted({self._abs.get(i, i) for i in idxs})
                active = [i for i in idxs if (self.store["segs"].get(self.uids[i]) or {}).get("locked")]
                if not active:
                    return 0
                before, undo = copy.deepcopy(self.store), list(self.undo)
                affected = {j for i in active for j in self.chain(i)}
                first = 0
                for part in self.store["parts"]:
                    last = self.idx_of.get(part["last"])
                    if last is None:
                        raise Oops("граница части не найдена; закрепление не изменено")
                    if part.get("locked") and any(first <= i <= last for i in affected):
                        part["locked"] = None
                        part.pop("auto", None)
                    first = last + 1
                try:
                    return super().unlock_segs(active, label)
                except Exception:
                    self.store, self.undo = before, undo
                    raise

        def new_tid(self, e):
            # Returning to an older project must not reuse numbers belonging
            # to a deleted take that is still recoverable from the journal.
            while True:
                tid = super().new_tid(e)
                sid = e["sid"]
                legacy = any((self.dir / "trash" / sid).glob(f"{sid}_{tid}__*"))
                durable = any((self.dir / "trash" / "operations").glob(f"*/takes/{sid}/{sid}_{tid}.*"))
                if not legacy and not durable:
                    return tid

        def _trash_find(self, sid, tid, ext="wav"):
            if not re.fullmatch(r"s\d+", str(sid)) or not re.fullmatch(r"t\d+", str(tid)) or ext not in ("wav", "json"):
                raise Oops("неверный идентификатор записи из корзины")
            found = []
            for path in (self.dir / "trash" / sid).glob(f"{sid}_{tid}__*.{ext}"):
                if path.is_file() and not path.is_symlink():
                    found.append(path)
            transactions = getattr(self, "_tx", None) or FileTransactions(self.dir)
            for path in (self.dir / "trash" / "operations").glob(f"*/takes/{sid}/{sid}_{tid}.{ext}"):
                op_id = path.relative_to(self.dir).parts[2]
                try:
                    record = transactions.manifest(op_id)
                    rel = path.relative_to(self.dir).as_posix()
                    move = next((m for m in record["moves"] if m["target"] == rel), None)
                    if record["state"] == "committed" and move and digest(path) == move["sha"]:
                        found.append(path)
                except (TransactionError, OSError, KeyError, TypeError) as exc:
                    raise Oops("не подтверждена запись в корзине: " + str(exc)) from exc
            if not found:
                return None
            versions = {digest(path) for path in found}
            if len(versions) != 1:
                raise Oops(f"в корзине несколько разных версий {sid}/{tid}; нужен ручной выбор, автоматическая подстановка отменена")
            return sorted(found)[-1]

        def _trash_back(self, sid, tid, ext):
            self.mutation_guard()
            source = self._trash_find(sid, tid, ext)
            if source is None:
                return False
            destination = self.takes_dir / sid / f"{sid}_{tid}.{ext}"
            if destination.exists():
                if digest(destination) != digest(source):
                    raise Oops("место возврата записи занято другим файлом")
                return True
            staging = self.dir / "transactions" / "staging" / uuid.uuid4().hex / destination.name
            staging.parent.mkdir(parents=True, exist_ok=False)
            shutil.copy2(source, staging)
            if digest(staging) != digest(source):
                raise Oops("копия записи из корзины не прошла проверку")
            tx = getattr(self, "_tx", None) or FileTransactions(self.dir)
            tx.commit({}, [(staging, destination)], label="запись возвращена из корзины проекта",
                      precondition=self.mutation_guard)
            self._takes = {}
            return True

        def _refresh_file_undo(self):
            have = {item[1] for item in self.undo if item[0] == "#files"}
            for record in self._tx.pending_undo():
                if record["id"] in have:
                    continue
                after = self._tx.snapshot_document(record["id"], "edits.json", "after") or {}
                if all(_same_entry(self.store["segs"].get(uid), after.get("segs", {}).get(uid)) for uid in record["uids"]):
                    self.undo.append(("#files", record["id"], record["label"]))
            del self.undo[:-80]

        def undo_last(self):
            self.mutation_guard()
            with self.lock:
                if not self.undo or self.undo[-1][0] != "#files":
                    return super().undo_last()
                _, op_id, label = self.undo[-1]
                record = self._tx.manifest(op_id)
                before = self._tx.snapshot_document(op_id, "edits.json", "before") or {}
                after = self._tx.snapshot_document(op_id, "edits.json", "after") or {}
                for uid in record["uids"]:
                    current = self.store["segs"].get(uid)
                    if (current or {}).get("locked"):
                        raise Oops("фраза закреплена; сначала снимите закрепление для отмены удаления")
                    if not _same_entry(current, after.get("segs", {}).get(uid)):
                        raise Oops("фраза менялась после удаления; сначала отмените последующие правки")
                restored = copy.deepcopy(self.store)
                for uid in record["uids"]:
                    previous = copy.deepcopy(before.get("segs", {}).get(uid))
                    if previous is None:
                        restored["segs"].pop(uid, None)
                    else:
                        previous["tseq"] = max(int(previous.get("tseq", 0)), int((restored["segs"].get(uid) or {}).get("tseq", 0)))
                        restored["segs"][uid] = previous
                try:
                    moves = self._tx.reverse_moves(op_id)
                except TransactionError as exc:
                    raise Oops(str(exc)) from exc
                self.store = restored
                self._commit_state("отмена: " + label, moves=moves, uids=record["uids"], undo_of=op_id)
                self.undo.pop()
                self.joins_refresh()
                self._refresh_file_undo()
                return record["uids"][0] if record["uids"] else "#files"

        def _take_bundle(self, sid, tid):
            if not re.fullmatch(r"s\d+", str(sid)) or not re.fullmatch(r"t\d+", str(tid)):
                raise Oops("неверный идентификатор дубля")
            wav = self.take_path(sid, tid)
            meta = g["read_json"](wav.with_suffix(".json"), None)
            if not wav.is_file() or not isinstance(meta, dict) or meta.get("tid") != tid:
                raise Oops("дубль неполон: нужны WAV и читаемые метаданные с тем же номером; файлы не изменены")
            return [wav, wav.with_suffix(".json")]

        def takes_of(self, e):
            if not e or not e.get("sid"):
                return {}
            sid = str(e["sid"])
            if not re.fullmatch(r"s\d+", sid):
                raise Oops("неверный номер папки дублей")
            folder = self.takes_dir / sid
            if folder.is_symlink():
                raise Oops("папка дублей не должна быть символической ссылкой")
            if not folder.is_dir():
                return {}
            files, fingerprint = [], []
            for meta in sorted(folder.glob("*.json")):
                match = re.fullmatch(re.escape(sid) + r"_(t\d+)\.json", meta.name)
                if not match or meta.is_symlink():
                    continue
                source = meta.with_suffix(".wav")
                try:
                    ms = meta.stat(); ws = source.stat()
                    if source.is_symlink() or not source.is_file():
                        continue
                except OSError:
                    continue
                files.append((meta, match[1]))
                fingerprint.append((meta.name, ms.st_mtime_ns, ms.st_size, ws.st_mtime_ns, ws.st_size))
            key = tuple(fingerprint)
            cached = self._takes.get(sid)
            if cached and cached[0] == key:
                return cached[1]
            result = {}
            for meta, tid in files:
                value = g["read_json"](meta, None)
                if isinstance(value, dict) and value.get("tid") == tid:
                    result[tid] = value
            self._takes[sid] = (key, result)
            return result

        def _trash_moves(self, paths, op_id):
            return [(path, self.dir / "trash" / "operations" / op_id / path.relative_to(self.dir)) for path in paths]

        def delete_take(self, uid, tid):
            self.mutation_guard()
            with self.lock:
                if uid not in self.idx_of:
                    raise Oops("фразы больше нет; обновите окно")
                e = self.store["segs"].get(uid) or {}
                if e.get("locked"):
                    raise Oops("фраза закреплена; сначала снимите закрепление")
                if any(job.get("uid") == uid and job.get("status") in ("queued", "running") for job in self.jobs.values()):
                    raise Oops("фраза сейчас озвучивается; дождитесь результата")
                paths = self._take_bundle(e.get("sid"), tid)
                op_id = uuid.uuid4().hex
                if e.get("take") == tid:
                    e.pop("take", None)
                (e.get("trims") or {}).pop(tid, None)
                if not e.get("trims"):
                    e.pop("trims", None)
                self._commit_state("дубль перенесён в корзину", {"uid": uid, "tid": tid},
                                   moves=self._trash_moves(paths, op_id), uids=[uid], undoable=True, op_id=op_id)

        def wipe_seg(self, uid):
            self.mutation_guard()
            with self.lock:
                if uid not in self.idx_of:
                    raise Oops("фразы больше нет; обновите окно")
                e = self.store["segs"].get(uid) or {}
                if e.get("locked"):
                    raise Oops("фраза закреплена; сначала снимите закрепление")
                if e.get("join") or e.get("into"):
                    raise Oops("сначала разъедините связанные фразы")
                if any(job.get("uid") == uid and job.get("status") in ("queued", "running") for job in self.jobs.values()):
                    raise Oops("фраза сейчас озвучивается; дождитесь результата")
                if not e:
                    raise Oops("у фразы нет правок или дублей")
                takes = self.takes_of(e)
                paths = [path for tid in takes for path in self._take_bundle(e["sid"], tid)]
                # Do not silently leave malformed audio out of "all takes".
                folder = self.takes_dir / e["sid"] if e.get("sid") else None
                known = set(paths)
                extras = [p for p in folder.glob("*") if p.suffix.lower() in (".wav", ".json") and p not in known] if folder else []
                if extras:
                    raise Oops("в папке фразы есть неполные дубли; сначала восстановите их метаданные")
                for key in set(self.RESET_KEYS) | {"trims", "mark", "ack"}:
                    e.pop(key, None)
                op_id = uuid.uuid4().hex
                self._commit_state("дубли и правки фразы убраны", {"uid": uid, "takes": len(takes)},
                                   moves=self._trash_moves(paths, op_id), uids=[uid], undoable=True, op_id=op_id)
                return dict(uid=uid, takes=len(takes), draft_preserved=True)

        def _referenced_takes(self):
            keep, whole_sids = set(), set()
            def walk(value, sid=None):
                if isinstance(value, dict):
                    sid = value.get("sid") or sid
                    for key, v in value.items():
                        if sid and isinstance(v, str) and re.fullmatch(r"t\d+", v):
                            keep.add((sid, v))
                        if key == "trims" and sid and isinstance(v, dict):
                            keep.update((sid, tid) for tid in v)
                        if key == "from_old" and isinstance(v, str) and re.fullmatch(r"s\d+/t\d+", v):
                            keep.add(tuple(v.split("/")))
                        walk(v, sid)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, sid)
            walk(self.store)
            for _, snap, _ in self.undo:
                if isinstance(snap, str):
                    try:
                        snap = json.loads(snap)
                    except ValueError:
                        continue
                walk(snap)
            for orphan in self.store.get("orphans", []):
                sid = (orphan.get("e") or {}).get("sid")
                if sid:
                    whole_sids.add(sid)
            # Existing no-audio snapshots must not lose their backing originals.
            project_dir = getattr(self, "proj_dir", self.dir / "projects")
            if project_dir.exists():
                for p in project_dir.rglob("*" + self.PROJ_EXT):
                    try:
                        with zipfile.ZipFile(p) as zf:
                            info = zf.getinfo("edits.json")
                            if info.file_size > 128 * 1024 * 1024:
                                raise Oops("слишком большой снимок правок: " + p.name)
                            doc = json.loads(zf.read(info))
                            walk(doc)
                            whole_sids.update(self._sids_of(doc))
                    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                        raise Oops("точка возврата не проверена; очистка отменена: " + p.name) from exc
            for meta in self.takes_dir.glob("*/*.json"):
                doc = g["read_json"](meta, None)
                if isinstance(doc, dict) and doc.get("from_old"):
                    walk({"from_old": doc["from_old"]})
            return keep, whole_sids

        def clean_takes(self, backup=True):
            self.mutation_guard()
            self.auto_guard("очистка дублей")
            if not self.rlock.acquire(timeout=120):
                return dict(skipped=True, removed=0, freed=0)
            try:
                with self.lock:
                    if any(j.get("status") in ("queued", "running") for j in self.jobs.values()):
                        raise Oops("дождитесь завершения переозвучки перед очисткой дублей")
                    keep, all_sid = self._referenced_takes()
                    files, takes = [], 0
                    for folder in self.takes_dir.iterdir():
                        if not folder.is_dir() or folder.is_symlink() or folder.name in all_sid:
                            continue
                        for meta in folder.glob("*.json"):
                            doc = g["read_json"](meta, None)
                            tid = (doc or {}).get("tid") if isinstance(doc, dict) else None
                            if not tid or (folder.name, tid) in keep:
                                continue
                            files.extend(self._take_bundle(folder.name, tid)); takes += 1
                    if backup:
                        self.project_save(note="перед очисткой дублей", audio=False, kind="manual")
                    op_id = uuid.uuid4().hex
                    size = sum(p.stat().st_size for p in files)
                    if files:
                        self._commit_state("невыбранные дубли перенесены в корзину", {"takes": takes},
                                           moves=self._trash_moves(files, op_id), undoable=True, op_id=op_id)
                    where = str(self.dir / "trash" / "operations" / op_id) if files else ""
            finally:
                self.rlock.release()
            freed = int(self.housekeeping(everything=True).get("freed") or 0)
            return dict(removed=len(files), takes=takes, freed=freed, quarantined_bytes=size,
                        backup=bool(backup), reversible=True, where=where)

        def reanchor(self, persist=True):
            self.mutation_guard()
            selected_protection = self._part_selected_guard_uids()
            by_key = collections.defaultdict(list)
            for i, key in enumerate(self.keys):
                by_key[key].append(i)
            before = copy.deepcopy(self.store)
            new, remap, lost, moved = {}, {}, 0, 0
            for uid, entry in self.store["segs"].items():
                e = copy.deepcopy(entry)
                context = e.get("ctx") or {}
                candidates = by_key.get(uid.split(".")[0], [])
                def score(i):
                    prev = self.keys[i - 1] if i else ""
                    nxt = self.keys[i + 1] if i + 1 < len(self.keys) else ""
                    return int("prev" in context and prev == context["prev"]) + int("next" in context and nxt == context["next"])
                current = self.idx_of.get(uid)
                best = None
                if len(candidates) == 1:
                    best = candidates[0]
                elif candidates:
                    maximum = max(map(score, candidates))
                    winners = [i for i in candidates if score(i) == maximum]
                    if maximum and len(winners) == 1:
                        best = winners[0]
                    elif current in winners and maximum == 2:
                        # Both old neighbours and the old occurrence still agree.
                        # A completely changed context is never guessed by index.
                        best = current
                if best is None or self.uids[best] in new:
                    if e.get("locked") or uid in selected_protection:
                        self.store = before
                        raise Oops("закреплённая фраза не сопоставлена однозначно: " + uid + "; прежние правки сохранены")
                    if not self.e_empty(e) or e.get("tseq"):
                        self.store["orphans"].append(dict(uid=uid, e=e, at=g["now"](), why="неоднозначная привязка"))
                        lost += 1
                    continue
                nu = self.uids[best]
                if (e.get("locked") or uid in selected_protection) and nu != uid:
                    self.store = before
                    raise Oops("перенумерован UID закреплённой фразы; нужна проверенная миграция редакции")
                moved += int(nu != uid)
                e["ctx"] = self.ctx_of(best)
                new[nu] = e
                remap[uid] = nu
            for entry in new.values():
                if entry.get("join"):
                    entry["join"] = [remap.get(u, u) for u in entry["join"]]
                if entry.get("into"):
                    entry["into"] = remap.get(entry["into"], entry["into"])
            self.store["segs"] = new
            self._abs = {}
            self._parts_cache = None
            dropped = self.joins_refresh()
            self.reanchor_cues()
            if persist and (moved or lost or dropped):
                self.save("привязка правок после переразбивки", {"moved": moved, "lost": lost, "dropped": dropped})

        def take_trim(self, uid, tid, t0=None, t1=None, spans=None):
            self.mutation_guard()
            with self.lock:
                index = self.idx_of.get(uid)
                if index is None:
                    raise Oops("фразы больше нет; обновите окно")
                e = self.store["segs"].get(uid) or {}
                if e.get("locked"):
                    raise Oops("фраза закреплена; сначала снимите закрепление")
                source = self.src(index) if not tid else None
                tk = self.takes_of(e).get(tid) if tid else None
                if tid and not tk:
                    raise Oops("такого дубля нет")
                if not tid and (not source or source.get("kind") != "draft"):
                    raise Oops("у фразы сейчас нет выбранного черновика")
                if not tid and e.get("join"):
                    raise Oops("сначала создайте дубль соединённой фразы")
                dur = float((tk or {}).get("dur") or 0)
                if not math.isfinite(dur) or dur <= 0:
                    audio, sr = self.read_audio(self.take_path(e["sid"], tid) if tid else source["path"])
                    dur = len(audio) / sr
                untrim = spans is None and t0 is None and t1 is None
                try:
                    selection = None if untrim else trim_spans(spans if spans is not None else
                                      [0. if t0 is None else t0, dur if t1 is None else t1], dur, strict=True)
                except (TypeError, ValueError) as exc:
                    raise Oops("неверная обрезка: " + str(exc)) from exc
                if selection is not None and trim_total(selection) < .15:
                    raise Oops("после обрезки остаётся меньше 0.15 секунды")
                new = (selection[0] if len(selection) == 1 else selection) if selection else None
                if selection and len(selection) == 1 and selection[0][0] < .005 and dur - selection[0][1] < .005:
                    new = None
                old = (e.get("trims") or {}).get(tid) if tid else None
                if new == old:
                    return
                self._push_undo(uid, "обрезка записи")
                if not tid:
                    e = self.E(uid, create=True)
                    tid = self._adopt(index, e, source)
                    e["take"] = tid
                if new is None:
                    (e.get("trims") or {}).pop(tid, None)
                    if not e.get("trims"):
                        e.pop("trims", None)
                else:
                    e.setdefault("trims", {})[tid] = new
                self.save("обрезка дубля", {"uid": uid, "tid": tid, "trim": new})

        def _stage_copy(self, source, destination, metadata):
            staging = self.dir / "transactions" / "staging" / uuid.uuid4().hex
            staging.mkdir(parents=True, exist_ok=False)
            audio = staging / "audio.wav"
            shutil.copy2(source, audio)
            if digest(audio) != digest(source):
                raise Oops("не прошла проверка копии аудиозаписи")
            meta = staging / "audio.json"
            atomic_json(meta, metadata)
            return [(audio, destination), (meta, destination.with_suffix(".json"))]

        def take_next(self, uid, tid, t0, t1, spans=None):
            self.mutation_guard()
            with self.lock:
                i = self.idx_of.get(uid)
                e = self.store["segs"].get(uid) or {}
                tk = self.takes_of(e).get(tid)
                if i is None or not tk or tk.get("src") != "import":
                    raise Oops("по фразам раскладывается только своя запись")
                j = self.chain(i)[-1] + 1
                if j >= len(self.rows) or self.part_of(j) != self.part_of(i):
                    raise Oops("это последняя фраза части")
                uid2 = self.uids[j]
                if (self.store["segs"].get(uid2) or {}).get("locked"):
                    raise Oops("следующая фраза закреплена; сначала снимите закрепление")
                dur = float(tk.get("dur") or 0)
                if dur <= 0:
                    audio, sr = self.read_audio(self.take_path(e["sid"], tid)); dur = len(audio) / sr
                try:
                    selected = trim_spans(spans if spans is not None else [t0 or 0., dur if t1 is None else t1], dur, strict=True)
                except ValueError as exc:
                    raise Oops(str(exc)) from exc
                end = (trim_spans(tk.get("trim0"), dur) or [[0., dur]])[-1][1]
                start = selected[-1][1]
                if end - start < .15:
                    raise Oops("после выбранной части осталось меньше 0.15 секунды; следующей фразе нечего передать")
                # All structural checks happen before changing the first phrase.
                self.take_trim(uid, tid, t0, t1, spans)
                self._push_undo(uid2, "своя запись: следующая фраза")
                e2 = self.E(uid2, create=True)
                ef2 = self.eff(j)
                tid2 = next((x["tid"] for x in self.takes_of(e2).values()
                             if x.get("src") == "import" and x.get("orig") and x.get("orig") == tk.get("orig") and self.fits(e2, x)), None)
                moves = []
                if tid2 is None:
                    tid2 = self.new_tid(e2)
                    meta = dict(tk, tid=tid2, created=g["now"](), role=ef2["role"], tech=ef2["tech"], raw=self.row_text(j),
                                source_raw=tk.get("source_raw") or tk.get("raw"), target_raw=self.row_text(j))
                    meta.pop("cover", None)
                    if e2.get("join"):
                        meta["cover"] = list(e2["join"])
                    moves = self._stage_copy(self.take_path(e["sid"], tid), self.take_path(e2["sid"], tid2), meta)
                if not (e2.get("trims") or {}).get(tid2):
                    e2.setdefault("trims", {})[tid2] = [round(start, 3), round(end, 3)]
                e2["take"] = tid2
                self._commit_state("своя запись: следующая фраза", {"uid": uid2, "tid": tid2}, moves=moves)
                return dict(uid=uid2, tid=tid2)

        @staticmethod
        def _tnorm(text):
            # Accents/case and whitespace are orthographic, punctuation/ё are
            # kept for automatic exact matches. Fuzzy matches remain manual.
            return " ".join(unicodedata.normalize("NFC", str(text or "")).replace("\u0301", "").casefold().split())

        def pipeline_old_voices(self):
            manifests = list((self.root / "work" / "freeze_backups").glob("*/manifest.json"))
            manifests += list((self.root / "work" / "audio_history").glob("*/manifest.json"))
            observed = [p for manifest in manifests for p in
                        (manifest, manifest.parent / "work/segments.csv", manifest.parent / "work/roles.json") if p.exists()]
            key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in observed)
            cache = getattr(self, "_pipeline_voice_cache", None)
            if cache and cache[0] == key:
                return cache[1]
            records, by_sha, errors = [], collections.defaultdict(list), []
            for path in sorted(manifests):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8-sig"))
                    revision = str(doc.get("revision") or path.parent.name)
                    token = hashlib.sha256(str(path.relative_to(self.root)).encode()).hexdigest()[:16]
                    if (doc.get("state") in ("committed", "prepared", "rolled_back") and doc.get("catalog_ready")
                            and isinstance(doc.get("pipeline_audio"), list)):
                        for rel in ("work/segments.csv", "work/roles.json"):
                            expected = (doc.get("files") or {}).get(rel)
                            if not expected or digest(path.parent / rel) != expected:
                                raise ValueError("не прошла проверка исходной редакции: " + rel)
                        with (path.parent / "work/segments.csv").open(encoding="utf-8-sig", newline="") as handle:
                            old_rows = list(csv.DictReader(handle, delimiter=";"))
                        old_roles = json.loads((path.parent / "work/roles.json").read_text(encoding="utf-8-sig"))
                        if (not isinstance(old_roles, dict) or not isinstance(old_roles.get("roles"), dict)
                                or not isinstance(old_roles.get("aliases", {}), dict)
                                or any(not isinstance(role, dict) for role in old_roles["roles"].values())):
                            raise ValueError("повреждён сохранённый кастинг ролей")
                        from nox_freeze import row_uids
                        old_uids = row_uids(old_rows)
                        pending = []
                        for index, item in enumerate(doc["pipeline_audio"]):
                            if not all(item.get(field) for field in ("text", "role", "wav", "sha256")):
                                continue
                            placement = int(item["index"])
                            if not 0 <= placement < len(old_rows) or old_rows[placement]["text"] != item["text"]:
                                raise ValueError("запись не соответствует CSV прежней редакции")
                            if item.get("uid") != old_uids[placement]:
                                raise ValueError("UID записи не соответствует CSV прежней редакции")
                            # Resolve the saved role with the same runtime rule
                            # that produced the snapshot, never today's roles.
                            expected_role = self._role_of(old_rows[placement], old_roles)
                            if item["role"] != expected_role:
                                raise ValueError("роль записи не соответствует сохранённому кастингу")
                            raw = str(item["text"])
                            rec = dict(sid="p" + token, tid=f"t{index + 1:06d}", uid=item.get("uid") or "",
                                       raw=raw, source_raw=raw, norm=self._tnorm(raw), role=str(item["role"]),
                                       dur=item.get("dur"), src="pipeline_history", at=revision, revision=revision,
                                       source_kind="pipeline", source_sha256=item["sha256"], source_paths=[item["wav"], item.get("retained_wav")],
                                       source_tech=item.get("source_tech"), source_sidecars=item.get("sidecars") or {},
                                       context={}, provenance_complete=True)
                            if not rec["source_tech"]:
                                side = rec["source_sidecars"].get("txt") or {}
                                for value in (side.get("path"), side.get("retained_path")):
                                    if value and side.get("sha256"):
                                        text_path = (self.root / value).resolve()
                                        if (self.root.resolve() in text_path.parents and text_path.is_file()
                                                and text_path.stat().st_size < 1_000_000 and digest(text_path) == side["sha256"]):
                                            rec["source_tech"] = text_path.read_text(encoding="utf-8-sig")
                                            break
                            pending.append(rec)
                        # A bad record rejects this complete manifest, including
                        # records that occurred before the damaged entry.
                        records.extend(pending)
                        for rec in pending:
                            by_sha[rec["source_sha256"]].append(rec)
                    elif doc.get("state") == "archived" and doc.get("kind") == "pipeline_audio":
                        wavs = [f for f in doc.get("files", []) if str(f.get("name", "")).lower().endswith(".wav")]
                        for index, item in enumerate(wavs):
                            location = path.parent / item["name"]
                            if location.resolve().parent != path.parent.resolve():
                                raise ValueError("неверный путь WAV в архиве")
                            raw = str(doc.get("source_tech") or "")
                            records.append(dict(sid="h" + token, tid=f"t{index + 1:06d}", uid="", raw=raw or "[текст исходной записи неизвестен]",
                                                source_raw=None, norm=self._tnorm(raw), role=None, dur=None,
                                                src="pipeline_history", at=revision, revision=revision,
                                                source_kind="pipeline", source_sha256=item.get("sha256"),
                                                source_paths=[str(location.relative_to(self.root))], source_tech=raw,
                                                context={}, provenance_complete=False))
                    elif doc.get("state") in ("publishing", "recovery_required"):
                        errors.append(dict(file=str(path), error="незавершённое сохранение прежней редакции; требуется восстановление"))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(dict(file=str(path), error=str(exc)))
            # An archived stem can be linked to a verified revision by audio
            # SHA; neither its target_row nor the filename is treated as text.
            for rec in records:
                matches = by_sha.get(rec.get("source_sha256"), [])
                identities = {(v.get("uid"), v.get("source_raw"), v.get("role")) for v in matches}
                known = matches[0] if len(identities) == 1 else None
                if known and not rec["provenance_complete"]:
                    for field in ("raw", "source_raw", "norm", "role", "uid"):
                        rec[field] = known[field]
                    rec["provenance_complete"] = True
                    rec["source_revision"] = known["revision"]
                elif matches and not rec["provenance_complete"]:
                    rec["provenance_ambiguous"] = True
            # Several references to the same WAV are still distinct historical
            # placements. Exact duplicates of one placement need only one entry.
            unique = {}
            for rec in records:
                identity = (rec.get("source_sha256"), rec.get("uid"), rec.get("source_raw"), rec.get("role"))
                if identity not in unique or (not unique[identity]["provenance_complete"] and rec["provenance_complete"]):
                    unique[identity] = rec
                    rec["source_revisions"] = [rec.get("source_revision") or rec["revision"]]
                else:
                    unique[identity]["source_paths"].extend(rec["source_paths"])
                    for field in ("source_tech", "source_sidecars"):
                        if not unique[identity].get(field) and rec.get(field):
                            unique[identity][field] = rec[field]
                    revision = rec.get("source_revision") or rec["revision"]
                    if revision not in unique[identity]["source_revisions"]:
                        unique[identity]["source_revisions"].append(revision)
            result = list(unique.values())
            self._pipeline_voice_cache = (key, result)
            self._old_voice_errors = errors
            return result

        def _old_source_path(self, record):
            if record.get("source_kind") != "pipeline":
                return self._take_bundle(record["sid"], record["tid"])[0]
            root = self.root.resolve()
            for value in record.get("source_paths") or []:
                if not value:
                    continue
                raw = Path(value)
                p = (raw if raw.is_absolute() else root / raw).resolve()
                work = root / "work"
                if work not in p.parents or p.suffix.lower() != ".wav":
                    continue
                if p.is_file() and record.get("source_sha256") and digest(p) == record["source_sha256"]:
                    return p
            raise Oops("проверенная аудиозапись этой редакции не найдена или её байты изменились")

        def old_voices(self):
            candidates = []
            # Metadata is small; don't cache a key that ignores nested changes.
            for orphan in self.store.get("orphans", []):
                entry = orphan.get("e") or {}
                sid = entry.get("sid")
                if not sid or not re.fullmatch(r"s\d+", str(sid)):
                    continue
                for f in sorted((self.takes_dir / sid).glob("*.json")):
                    meta = g["read_json"](f, None)
                    if not isinstance(meta, dict) or not re.fullmatch(r"t\d+", str(meta.get("tid", ""))):
                        continue
                    tid = meta["tid"]
                    if not meta.get("raw") or meta.get("cover") or not self.take_path(sid, tid).is_file():
                        continue
                    source_text = str(meta.get("source_raw") or meta["raw"])
                    candidates.append(dict(sid=sid, tid=tid, uid=orphan["uid"], raw=source_text,
                                           source_raw=source_text, norm=self._tnorm(source_text), dur=meta.get("dur"),
                                           role=meta.get("role"), src=meta.get("src"), at=orphan.get("at"),
                                           source_tech=meta.get("tech"),
                                           context=entry.get("ctx") or {}, provenance_complete=True))
            return candidates + self.pipeline_old_voices()

        def old_voice_for(self, uid, limit=8, offset=0, with_info=False):
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("фразы больше нет; обновите окно")
            target = self._tnorm(self.row_text(i))
            if not target:
                return dict(items=[], total=0, offset=0, limit=limit, next=None, errors=[]) if with_info else []
            effective = self.eff(i)
            role = effective["role"]
            hits = []
            for voice in self.old_voices():
                text = voice["norm"]
                if not text:
                    hits.append(dict(voice, ratio=0., exact=False, role_match=False, unknown_text=True,
                                     text="Текст прежней записи неизвестен; можно прослушать и выбрать вручную"))
                    continue
                if abs(len(text) - len(target)) > max(6, .5 * len(target)):
                    continue
                ratio = difflib.SequenceMatcher(None, target, text, autojunk=False).ratio()
                if ratio >= .6:
                    hits.append(dict(voice, ratio=round(ratio, 4), exact=text == target and voice.get("provenance_complete", True),
                                     role_match=bool(voice.get("role") and voice["role"] == role), text=voice["raw"][:300]))
            exact = [v for v in hits if v["exact"] and v["role_match"]]
            target_count = sum(1 for j in range(len(self.rows)) if self._tnorm(self.row_text(j)) == target
                               and self.eff(j)["role"] == role)
            for voice in hits:
                voice["ambiguous"] = len(exact) != 1 or target_count != 1
                voice["tech_match"] = bool(voice.get("source_tech") and
                                            self._tnorm(voice["source_tech"]) == self._tnorm(effective["tech"]))
                voice["auto_safe"] = bool(voice["exact"] and voice["role_match"] and voice["tech_match"]
                                           and not voice["ambiguous"])
                voice.pop("norm", None)
            hits.sort(key=lambda v: (not v["exact"], not v["role_match"], -v["ratio"], str(v.get("at") or ""), v["sid"], v["tid"]))
            offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
            view = hits[offset:offset + limit]
            if with_info:
                return dict(items=view, total=len(hits), offset=offset, limit=limit,
                            next=offset + len(view) if offset + len(view) < len(hits) else None,
                            errors=getattr(self, "_old_voice_errors", []))
            return view

        def old_voice_adopt(self, uid, sid, tid, choose=False, _save=True, automatic=False):
            self.mutation_guard()
            with self.lock:
                i = self.idx_of.get(uid)
                if i is None:
                    raise Oops("фразы больше нет; обновите окно")
                e = self.store["segs"].get(uid) or {}
                if e.get("locked") or e.get("join") or e.get("into"):
                    raise Oops("сначала снимите закрепление или разъедините фразу")
                candidate = next((v for v in self.old_voices() if v["sid"] == sid and v["tid"] == tid), None)
                if candidate is None:
                    raise Oops("прежний дубль больше не найден")
                if automatic and not any(v["sid"] == sid and v["tid"] == tid and v["auto_safe"]
                                         for v in self.old_voice_for(uid, 100)):
                    raise Oops("не подтверждено единственное совпадение роли и нынешнего текста для озвучки")
                for meta in self.takes_of(e).values():
                    if meta.get("from_old") == f"{sid}/{tid}":
                        if choose:
                            self.choose_take(uid, meta["tid"])
                        return dict(uid=uid, tid=meta["tid"], dup=True, selected=bool(choose))
                source = self._old_source_path(candidate)
                self._push_undo(uid, "прежняя озвучка возвращена")
                e = self.E(uid, create=True)
                target_tid = self.new_tid(e)
                meta = g["read_json"](source.with_suffix(".json"), {}) if candidate.get("source_kind") != "pipeline" else {}
                if candidate.get("source_kind") == "pipeline":
                    audio, sample_rate = self.read_audio(source)
                    tech = candidate.get("source_tech")
                    side = (candidate.get("source_sidecars") or {}).get("txt") or {}
                    if not tech and side.get("sha256"):
                        for rel in (side.get("path"), side.get("retained_path")):
                            if rel:
                                text_file = (self.root / rel).resolve()
                                if self.root.resolve() in text_file.parents and digest(text_file) == side["sha256"]:
                                    tech = text_file.read_text(encoding="utf-8-sig")
                                    break
                    meta = dict(src="recovered_pipeline", role=candidate.get("role"), tech=tech,
                                raw=candidate.get("source_raw"), dur=len(audio) / sample_rate)
                target_raw, source_raw = self.row_text(i), candidate["source_raw"]
                out = dict(meta, tid=target_tid, created=g["now"](), raw=source_raw, source_raw=source_raw,
                           target_raw=target_raw, from_old=f"{sid}/{tid}", source_uid=candidate["uid"],
                           source_sha256=digest(source), adopted_by="automatic" if automatic else "manual", source_role=meta.get("role"),
                           target_role=self.eff(i)["role"], source_revision=candidate.get("source_revision") or candidate.get("revision"),
                           source_revisions=candidate.get("source_revisions") or [],
                           provenance_complete=candidate.get("provenance_complete", True))
                out.pop("cover", None)
                moves = self._stage_copy(source, self.take_path(e["sid"], target_tid), out)
                if choose:
                    e["take"] = target_tid
                    # Same behavior as choose_take, while preserving the source
                    # text and marking a mismatched target as stale for review.
                    if out.get("role") and out["role"] in self.roles["roles"]:
                        if out["role"] != self.base_roles[i]:
                            e["role"] = out["role"]
                        else:
                            e.pop("role", None)
                    if out.get("tech"):
                        e["tech"] = out["tech"]
                self._commit_state("прежняя озвучка возвращена", {"uid": uid, "tid": target_tid}, moves=moves)
                return dict(uid=uid, tid=target_tid, selected=bool(choose), source_raw=source_raw,
                            target_raw=target_raw, exact=self._tnorm(source_raw) == self._tnorm(target_raw))

        def old_voice_auto(self, k):
            got = skipped = 0
            for i in self.part_range(k):
                uid = self.uids[i]
                e = self.store["segs"].get(uid) or {}
                if e.get("locked") or e.get("join") or e.get("into") or self.rows[i].get("ins"):
                    continue
                if self.takes_of(e) or self.wavs[i].exists():
                    continue
                candidates = self.old_voice_for(uid, 100)
                exact = [v for v in candidates if v["auto_safe"]]
                if len(exact) != 1:
                    skipped += bool(candidates)
                    continue
                self.old_voice_adopt(uid, exact[0]["sid"], exact[0]["tid"], choose=True, automatic=True)
                got += 1
            return dict(got=got, skipped=skipped)

        def old_voice_path(self, sid, tid):
            candidate = next((v for v in self.old_voices() if v["sid"] == sid and v["tid"] == tid), None)
            if candidate is None:
                raise Oops("этого прежнего дубля нет в каталоге")
            return self._old_source_path(candidate)

        def ext_roots(self):
            p = self.dir / "sound_roots.json"
            if not p.exists():
                return []
            try:
                doc = json.loads(p.read_text(encoding="utf-8-sig"))
                roots = doc["roots"]
                if not isinstance(roots, list) or any(not isinstance(r, dict) or not r.get("id") or not r.get("path") for r in roots):
                    raise ValueError("неверный список корней")
                return roots
            except (OSError, ValueError, KeyError) as exc:
                raise Oops("sound_roots.json не читается; исходный список не изменён") from exc

        def ext_save(self, roots):
            self.mutation_guard()
            atomic_json(self.dir / "sound_roots.json", {"roots": roots})
            self._ext = None

        def ext_catalog(self, fresh=False):
            roots = self.ext_roots()
            key = json.dumps(roots, ensure_ascii=False, sort_keys=True)
            cached = getattr(self, "_ext", None)
            if not fresh and cached and cached[0] == key and time.monotonic() - cached[1] < 30:
                return cached[2]
            items, scan = bounded_scan(roots, g["SND_EXT"], file_cap=self.EXT_CAP)
            self._ext = (key, time.monotonic(), items, scan)
            return items

        def sound_refresh(self):
            self._cat = None
            self._ext = None
            self.ext_catalog(fresh=True)
            return self.sound_tree()

        def kit_catalog(self):
            items = super().kit_catalog()
            for x in items:
                if "sec" in x:
                    continue
                parts = Path(x["file"].removeprefix("kit:")).parts
                category = next((p for p in parts if p in g["KIT_CAT"]), "")
                x["sec"] = g["KIT_CAT"].get(category) or {"mus": "музыка", "amb": "фон местности", "dlg": "реплики игры"}.get(x.get("kind"), "прочее")
                at = parts.index(category) if category else -1
                x["grp"] = ENT_RU.get(parts[at + 1].lower(), parts[at + 1]) if at >= 0 and len(parts) > at + 2 else ""
                entity, action = sfx_describe(Path(parts[-1]).stem)
                if entity or action:
                    human = (entity.capitalize() + (": " + action if action else "")) if entity else action
                    if x.get("title") in (x.get("name"), Path(parts[-1]).stem):
                        x["title"] = human
                    x["hay"] = g["s_norm"](" ".join((x.get("hay", ""), human, x["sec"], x["grp"])))
                    x["words"] = frozenset(x["hay"].split())
            return items

        def _sound_entries(self):
            my, index = [], self.sound_index()
            if self.sounds_dir.is_dir():
                for f in self.sounds_dir.iterdir():
                    if f.is_file() and not f.is_symlink() and f.suffix.lower() in g["SND_EXT"]:
                        meta = index.get(f.name) or {}
                        name = meta.get("name") or f.name
                        hay = g["s_norm"](name + " " + f.name)
                        my.append(dict(file=f.name, name=name, title=name, sub="мой файл", kind="", dur=meta.get("dur"),
                                       lib="my", rank=-1, hay=hay, words=frozenset(hay.split()), sec="мои файлы", grp=""))
            return my + self.kit_catalog() + self.ext_catalog()

        def sound_list(self, q="", kind="", sec="", grp="", offset=0, limit=80):
            all_items = self._sound_entries()
            entries = [x for x in all_items if (not sec or x.get("sec") == sec) and (not grp or x.get("grp") == grp)]
            pref = lambda x: 0 if x["lib"] == "my" or not kind or x["kind"] == kind else 1
            def search(text):
                words = g["s_norm"](text).split()
                if not words:
                    return None
                scores = [(g["s_score"](words, x["words"], x["hay"]), x) for x in entries]
                return [(score, item) for score, item in scores if score > 0]
            hits, fixed = search(q), ""
            if hits is not None and not hits:
                for alternative in (str(q).lower().translate(g["_EN2RU"]), str(q).lower().translate(g["_RU2EN"])):
                    candidate = search(alternative)
                    if candidate:
                        hits, fixed = candidate, alternative
                        break
            partial = False
            if hits is not None and not hits and len(g["s_norm"](q).split()) > 1:
                hits = [(sum(g["s_score"]([word], x["words"], x["hay"]) for word in g["s_norm"](q).split()), x) for x in entries]
                hits = [(score, item) for score, item in hits if score > 0]
                partial = bool(hits)
            result = sorted(entries, key=lambda x: (x["rank"], pref(x), x["title"])) if hits is None else [x for _, x in sorted(hits, key=lambda pair: (-pair[0], pref(pair[1]), pair[1]["rank"]))]
            offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
            view = [{k: v for k, v in item.items() if k not in ("hay", "words", "rank")} for item in result[offset:offset + limit]]
            kit = self.kit_active()
            return dict(sounds=view, total=len(result), offset=offset, limit=limit,
                        next=offset + len(view) if offset + len(view) < len(result) else None,
                        all=len(all_items), fixed=fixed, partial=partial, kit=str(kit) if kit else None,
                        scan=(getattr(self, "_ext", None) or (None, None, None, {}))[3])

        def sound_tree(self):
            sections = {}
            for item in self._sound_entries():
                section = sections.setdefault(item["sec"], {"n": 0, "groups": collections.Counter()})
                section["n"] += 1
                if item.get("grp"):
                    section["groups"][item["grp"]] += 1
            order = ["мои файлы", "музыка", "фон местности", "существа", "заклинания", "посохи и зелья", "оружие", "предметы", "герой", "окружение", "интерфейс и сюжет", "разное", "реплики игры", "прочее"]
            names = sorted(sections, key=lambda name: (order.index(name) if name in order else 99, name))
            return dict(sections=[dict(sec=name, n=sections[name]["n"], groups=[dict(grp=group, n=count) for group, count in sorted(sections[name]["groups"].items())]) for name in names],
                        roots=self.ext_roots(), scan=(getattr(self, "_ext", None) or (None, None, None, {}))[3])

        def sound_root_add(self, path, name=""):
            self.mutation_guard()
            raw = Path(str(path).strip().strip('"'))
            if not raw.is_absolute() or not raw.is_dir():
                raise Oops("введите полный путь к существующей папке")
            p = raw.resolve()
            with self.lock:
                roots = self.ext_roots()
                if any(os.path.normcase(str(Path(r["path"]).resolve())) == os.path.normcase(str(p)) for r in roots):
                    raise Oops("эта папка уже подключена")
                rid = "e" + uuid.uuid4().hex[:12]
                roots.append(dict(id=rid, path=str(p), name=str(name or p.name)[:60]))
                self.ext_save(roots)
            self.ext_catalog()
            return dict(id=rid, files=sum(x["file"].startswith("ext:" + rid + "/") for x in self.ext_catalog()))

        def sound_root_del(self, rid):
            self.mutation_guard()
            with self.lock:
                prefix = "ext:" + rid + "/"
                references = [c for c in self.store["cues"] if str(c.get("file") or "").startswith(prefix)]
                references += [v for v in self.store.get("sound_map", {}).values() if isinstance(v, str) and v.startswith(prefix)]
                if references:
                    raise Oops("папка используется звуками книги; сначала замените их файлы на дорожках")
                self.ext_save([r for r in self.ext_roots() if r["id"] != rid])

        def sound_path(self, ref):
            if isinstance(ref, str) and ref.startswith("ext:"):
                rid, _, relative = ref[4:].partition("/")
                root = next((Path(r["path"]).resolve() for r in self.ext_roots() if r["id"] == rid), None)
                if root is None or not relative:
                    return None
                p = (root / relative).resolve()
                if root not in p.parents:
                    return None
                return p if p.is_file() and p.suffix.lower() in g["SND_EXT"] else None
            return super().sound_path(ref)

        def sound_name(self, ref):
            if isinstance(ref, str) and ref.startswith("ext:"):
                return ref.rsplit("/", 1)[-1]
            return super().sound_name(ref)

        def sound_title(self, ref):
            if isinstance(ref, str) and ref.startswith("ext:"):
                return next((x["title"] for x in self.ext_catalog() if x["file"] == ref), self.sound_name(ref))
            return super().sound_title(ref)

        def cues_scan_all(self):
            self.mutation_guard()
            parts = [p["n"] for p in self.plan() if p["opened"]]
            # Ready pipeline parts can be opened by the same existing scan API.
            parts += [p["n"] for p in self.plan() if not p["opened"] and all(self.wavs[i].exists() for i in range(p["a"], p["b"] + 1))]
            added = found = done = 0
            failed = []
            for number in parts:
                try:
                    result = self.cues_scan(number)
                    added += result["new"]; found += result["found"]; done += 1
                except Oops as exc:
                    failed.append(dict(part=number, error=str(exc)))
            return dict(parts=done, new=added, found=found, failed=failed)

        def release_kit_dir(self):
            for path in (self.root / "work" / "nox_release.json", self.dir / "nox_release.json",
                         Path(g["__file__"]).resolve().parent / "nox_release.json"):
                if path.exists():
                    try:
                        record = json.loads(path.read_text(encoding="utf-8-sig"))
                        kit = Path(record["kit_dir"])
                        if kit.is_dir() and (kit / "go_kniga1a.ps1").is_file():
                            return kit
                    except (OSError, ValueError, KeyError, TypeError):
                        raise Oops("повреждён путь установленного комплекта: " + str(path))
                    raise Oops("установленный комплект недоступен; проверьте kit_dir в " + str(path))
            return None

        def engine_view(self):
            result = super().engine_view()
            workers = result["workers"]
            result["book_workers"] = sum(w.get("mode") != "serve" for w in workers)
            result["editor_workers"] = sum(w.get("mode") == "serve" for w in workers)
            result["model_workers"] = len(workers)
            settings = g["read_json"](self.root / "work" / "fast_args.json", None) or {}
            try:
                limit = int(settings.get("max_new_tokens") or 1500)
                result["max_new_tokens"] = limit if limit > 0 else None
            except (ValueError, TypeError, AttributeError):
                result["max_new_tokens"] = None
            go = getattr(self, "go_proc", None)
            result["pipeline_starting"] = bool(go is not None and go.poll() is None and not workers)
            if result["pipeline_starting"]:
                result["starting"] = True
                result["note"] = "конвейер готовит озвучку; его воркеры примут заявки редактора после прогрева"
            return result

        def engine_start(self, force=False):
            self.mutation_guard()
            go = getattr(self, "go_proc", None)
            if not force and go is not None and go.poll() is None:
                # During preparation heartbeat files intentionally do not yet
                # declare a ready model. Starting f9 here needlessly loads a
                # fourth model beside the three forthcoming book workers.
                return self.engine_view()
            return super().engine_start(force)

        def tool_py(self, which="fast"):
            override = os.environ.get("NOX_TOOL_PY")
            if override:
                candidate = Path(override)
                if not candidate.is_file():
                    raise Oops("NOX_TOOL_PY указывает на отсутствующий Python")
                return str(candidate)
            home = Path(g["__file__"]).resolve().parent.parent
            environments = [self.root / ("env" if which == "design" else "envfast"), home / ("env" if which == "design" else "envfast")]
            for environment in environments:
                for relative in ("Scripts/python.exe", "python.exe", "bin/python"):
                    candidate = environment / relative
                    if candidate.is_file():
                        return str(candidate)
            if os.name != "nt":
                return sys.executable
            raise Oops("не найден Python среды " + ("env" if which == "design" else "envfast") + "; укажите NOX_TOOL_PY или восстановите окружение")

    return Studio
