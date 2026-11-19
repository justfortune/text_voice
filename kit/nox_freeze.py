# -*- coding: utf-8 -*-
"""Checked protection of an existing NOX edition; standard library only.

The pipeline CSV, Studio ``parts[].last`` and ``fixed_parts`` are separate
contracts.  No row number is used as a replacement for a missing UID.  Reading
this module never creates files.  Only ``preparation`` / ``commit_prepared_rows``
write, and neither moves, renames nor deletes an audio file.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProtectionError(RuntimeError):
    """Stop before changing an edition whose protection cannot be proved."""


FreezeMergeError = ProtectionError


@dataclass(frozen=True)
class ProtectionState:
    ranges: tuple[tuple[int, int, int], ...]  # a, b, part number; inclusive CSV indices
    protected_indices: frozenset[int]
    locked_uids: frozenset[str]
    derived_fixed: dict
    opened_ranges: tuple[tuple[int, int, int], ...] = ()
    # Older Studio allowed unlocking one phrase while its part stayed fixed.
    # These are real selected bundles, not fabricated individual snapshots.
    part_selected_uids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FreezeMergeResult:
    rows: list[dict]
    preserved_parts: int
    preserved_paragraphs: int
    preserved_segments: int
    new_segments: int
    last_old_row: int
    backup_path: str | None = None


def row_uids(rows):
    seen, out = Counter(), []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ProtectionError(f"segments.csv: строка {i + 1} без строкового text")
        h = hashlib.sha1(row["text"].encode("utf-8")).hexdigest()[:12]
        out.append(f"{h}.{seen[h]}")
        seen[h] += 1
    return out


def _int(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProtectionError(f"{label}: требуется целое число >= {minimum}")
    return value


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProtectionError(f"не читается {path}: {type(exc).__name__}: {exc}") from exc


def _edits(edits_doc):
    if edits_doc is None:
        return {"parts": [], "segs": {}, "ins": []}
    if not isinstance(edits_doc, dict):
        raise ProtectionError("edits.json: ожидается объект")
    for key, typ in (("parts", list), ("segs", dict)):
        if not isinstance(edits_doc.get(key), typ):
            raise ProtectionError(f"edits.json: поле {key} имеет неверный тип или отсутствует")
    if not isinstance(edits_doc.get("ins", []), list):
        raise ProtectionError("edits.json: ins должен быть списком")
    return edits_doc


def _part_locked(part, label):
    value = part.get("locked")
    if value is None or value is False or value == "":
        return False
    if not isinstance(value, str):
        raise ProtectionError(f"{label}: locked должен содержать дату закрепления")
    return True


def _insertion_anchors(st, pos):
    ins = {}
    for item in st.get("ins", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ProtectionError("edits.json: повреждена запись вставленной фразы")
        uid = item["id"]
        if uid in ins or uid in pos:
            raise ProtectionError(f"edits.json: повторный идентификатор вставки {uid}")
        ins[uid] = item
    anchors = {}
    def resolve(uid, trail):
        if uid in anchors:
            return anchors[uid]
        if uid in trail:
            raise ProtectionError("edits.json: циклические привязки вставок")
        after = ins[uid].get("after")
        if not isinstance(after, str):
            raise ProtectionError(f"вставка {uid}: after должен быть строковым UID")
        if after == "":
            a = -1
        elif after in pos:
            a = pos[after]
        elif after in ins:
            a = resolve(after, trail | {uid})
        else:
            raise ProtectionError(f"вставка {uid}: исходный UID after отсутствует; after_id не используется как замена")
        anchors[uid] = a
        return a
    for uid in ins:
        resolve(uid, set())
    return ins, anchors


def _validate_locks(rows, st, pos):
    segs, locked, protected = st["segs"], set(), set()
    ins, ins_anchors = _insertion_anchors(st, pos)
    for uid, e in segs.items():
        if not isinstance(uid, str) or not isinstance(e, dict):
            raise ProtectionError("edits.json: segs содержит повреждённую запись")
        lk = e.get("locked")
        if lk is None or lk is False:
            continue
        if not isinstance(lk, dict) or not lk:
            raise ProtectionError(f"фраза {uid}: locked должен быть непустым объектом")
        if uid not in pos and uid not in ins:
            raise ProtectionError(f"закреплённая фраза {uid} отсутствует в разбивке и вставках")
        locked.add(uid)
        i = pos.get(uid, ins_anchors.get(uid, -1))
        if i >= 0:
            protected.add(i)
        if lk.get("head"):
            head = lk["head"]
            if not isinstance(head, str):
                raise ProtectionError(f"фраза {uid}: head должен быть строковым UID")
            he = segs.get(head)
            hl = he.get("locked") if isinstance(he, dict) else None
            if (not isinstance(hl, dict) or not hl.get("take") or hl.get("head")
                    or e.get("into") != head or uid not in (he.get("join") or [])):
                raise ProtectionError(f"закреплённая присоединённая фраза {uid}: не подтверждена головная {head}")
        elif not isinstance(lk.get("take"), str) or not lk["take"]:
            raise ProtectionError(f"закреплённая фраза {uid}: не указан сохранённый take")
        elif e.get("take") not in (None, lk["take"]):
            raise ProtectionError(f"закреплённая фраза {uid}: выбранный take отличается от locked.take")
        if e.get("join"):
            js = e["join"]
            if (not isinstance(js, list) or not all(isinstance(u, str) for u in js)
                    or uid not in pos or len(set(js)) != len(js)):
                raise ProtectionError(f"закреплённое соединение {uid}: повреждён список join")
            head_i = pos[uid]
            for offset, child in enumerate(js, 1):
                ce = segs.get(child)
                cl = ce.get("locked") if isinstance(ce, dict) else None
                if (pos.get(child) != head_i + offset or not isinstance(cl, dict)
                        or cl.get("head") != uid or ce.get("into") != uid):
                    raise ProtectionError(f"закреплённое соединение {uid}: нарушена цепочка {child}")
            if any(head_i <= a < head_i + len(js) for a in ins_anchors.values()):
                raise ProtectionError(f"внутри закреплённого соединения {uid} появилась вставка")
    return frozenset(locked), frozenset(protected)


def _part_members(uids, anchors, start, end):
    return list(uids[start:end + 1]) + [uid for uid, anchor in anchors.items()
                                      if start <= anchor <= end or (start == 0 and anchor == -1)]


def _validate_part_selection(st, uid, number, *, insertion=False):
    """Only the explicit selected-take legacy state is accepted inside a fixed part.

    A part flag protects its range independently of individual locks. It does
    not prove a missing source, an unresolved join or an inferred raw draft.
    No values are inserted into edits.json to make this state look locked.
    """
    label = f"часть {number}: не закреплена входящая " + ("авторская вставка " if insertion else "фраза ") + uid
    entry = st["segs"].get(uid)
    if not isinstance(entry, dict) or not entry.get("sid") or not entry.get("take"):
        raise ProtectionError(label + "; для совместимости требуется явно выбранный сохранённый дубль (sid и take)")
    if entry.get("join") or entry.get("into"):
        raise ProtectionError(label + "; состав незакреплённой склейки не подтверждён")
    _component(entry["sid"], "sid выбранной записи закреплённой части")
    _component(entry["take"], "take выбранной записи закреплённой части")


def fixed_document(rows, edits_doc):
    """Derive fixed ranges from the REAL Studio schema, without disk access.

    ``last_p`` and ``last_i`` are informational only. All opened part boundaries
    must resolve monotonically. A row of a fixed part has an individual lock,
    a valid joined-child lock, or an explicit selected bundle retained by an
    older Studio after an individual unlock. No lock is manufactured here.
    """
    uids = row_uids(rows)
    pos = {u: i for i, u in enumerate(uids)}
    st = _edits(edits_doc)
    locked, _ = _validate_locks(rows, st, pos)
    ins, ins_anchors = _insertion_anchors(st, pos)
    out, start = [], 0
    for k, p in enumerate(st["parts"], 1):
        if not isinstance(p, dict) or _int(p.get("n", k), f"часть {k}: n", 1) != k:
            raise ProtectionError(f"edits.json: части идут не по порядку у части {k}")
        last = p.get("last")
        if isinstance(last, str) and last in ins:
            raise ProtectionError(f"часть {k}: last указывает на авторскую вставку {last}, а не на исходную реплику; "
                                  "CSV-граница такой части не подтверждена. Вставка и правки оставлены без изменений")
        if not isinstance(last, str) or last not in pos or pos[last] < start:
            raise ProtectionError(f"часть {k}: last UID отсутствует или нарушает порядок; числовая подстановка запрещена")
        b = pos[last]
        if _part_locked(p, f"часть {k}"):
            for uid in _part_members(uids, ins_anchors, start, b):
                if uid not in locked:
                    _validate_part_selection(st, uid, k, insertion=uid in ins)
            out.append(dict(n=k, a=start, b=b, a_uid=uids[start], b_uid=last))
        start = b + 1
    return {"ver": 1, "parts": out}


def validate_protection(rows, fixed_doc=None, edits_doc=None):
    """Validate both documents against the supplied *pipeline* CSV rows.

    None means the document is absent, not unreadable. If either document
    contains part locks, the other is mandatory. Individually locked phrases
    are protected even when no part is locked. Non-prefix fixed parts are valid.
    """
    uids = row_uids(rows)
    pos = {u: i for i, u in enumerate(uids)}
    st = _edits(edits_doc)
    derived = fixed_document(rows, st)
    locked, protected = _validate_locks(rows, st, pos)
    if fixed_doc is None:
        if derived["parts"]:
            raise ProtectionError("edits.json содержит закреплённые части, но fixed_parts.json отсутствует")
        fixed_doc = {"ver": 1, "parts": []}
    if (not isinstance(fixed_doc, dict) or not isinstance(fixed_doc.get("parts"), list)
            or fixed_doc.get("ver", 1) != 1):
        raise ProtectionError("fixed_parts.json: требуется объект версии 1 с массивом parts")
    parts, ranges, previous_b, previous_n = [], [], -1, 0
    for p in fixed_doc["parts"]:
        if not isinstance(p, dict):
            raise ProtectionError("fixed_parts.json: повреждена запись части")
        number = _int(p.get("n"), "fixed_parts.n", 1)
        a, b = _int(p.get("a"), f"часть {number}: a"), _int(p.get("b"), f"часть {number}: b")
        if a <= previous_b or number <= previous_n or not a <= b < len(rows):
            raise ProtectionError(f"часть {number}: пересечение, неправильный порядок или границы [{a}, {b}]")
        if p.get("a_uid") != uids[a] or p.get("b_uid") != uids[b]:
            raise ProtectionError(f"часть {number}: UID границ не совпадают с segments.csv")
        parts.append({key: p[key] for key in ("n", "a", "b", "a_uid", "b_uid")})
        ranges.append((a, b, number))
        previous_b, previous_n = b, number
    if parts and edits_doc is None:
        raise ProtectionError("fixed_parts.json содержит закрепления, но edits.json отсутствует")
    if parts != derived["parts"]:
        raise ProtectionError("fixed_parts.json и edits.parts[].last/locked описывают разные закрепления")
    opened, start = [], 0
    for k, p in enumerate(st["parts"], 1):
        b = pos[p["last"]]  # fixed_document has already validated the UID/order
        opened.append((start, b, k))
        start = b + 1
    _ins, anchors = _insertion_anchors(st, pos)
    protected = set(protected)
    selected = set()
    for a, b, _number in ranges:
        protected.update(range(a, b + 1))
        selected.update(uid for uid in _part_members(uids, anchors, a, b) if uid not in locked)
    return ProtectionState(tuple(ranges), frozenset(protected), locked, derived, tuple(opened), frozenset(selected))


def _component(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ProtectionError(f"небезопасный идентификатор {label}: {value!r}")
    return value


def locked_audio_paths(root, edits_doc, state):
    """Validate protected bundles, including explicit legacy part selections.

    The original name is kept for callers. Joined children legitimately have
    no independent WAV; part selections keep their real unlocked edit state.
    """
    result = []
    for uid in sorted(state.locked_uids):
        e = edits_doc["segs"][uid]
        lk = e["locked"]
        if lk.get("head"):
            continue
        sid, tid = _component(e.get("sid"), "sid"), _component(lk["take"], "take")
        wav = Path(root) / "studio" / "takes" / sid / f"{sid}_{tid}.wav"
        meta_path = wav.with_suffix(".json")
        if not wav.is_file() or wav.stat().st_size <= 44:
            raise ProtectionError(f"закреплённая фраза {uid}: отсутствует или пуст {wav}")
        meta = _read_json(meta_path)
        if (not isinstance(meta, dict) or meta.get("tid") != tid
                or not isinstance(meta.get("cover", []), (list, type(None)))
                or list(meta.get("cover") or []) != list(e.get("join") or [])):
            raise ProtectionError(f"закреплённая фраза {uid}: JSON дубля не соответствует take/join")
        result.extend((wav, meta_path))
    return result + part_selected_audio_paths(root, edits_doc, state)


def part_selected_audio_paths(root, edits_doc, state):
    """Read-only verification of explicit selections inside fixed parts."""
    result = []
    for uid in sorted(state.part_selected_uids):
        e = edits_doc["segs"][uid]
        sid, tid = _component(e.get("sid"), "sid"), _component(e.get("take"), "take")
        wav = Path(root) / "studio" / "takes" / sid / f"{sid}_{tid}.wav"
        meta_path = wav.with_suffix(".json")
        if not wav.is_file() or wav.stat().st_size <= 44:
            raise ProtectionError(f"выбранная запись закреплённой части {uid}: отсутствует или пуст {wav}")
        meta = _read_json(meta_path)
        if (not isinstance(meta, dict) or meta.get("tid") != tid
                or not isinstance(meta.get("cover", []), (list, type(None))) or meta.get("cover")):
            raise ProtectionError(f"выбранная запись закреплённой части {uid}: JSON дубля не соответствует выбранному take/составу")
        result.extend((wav, meta_path))
    return result


def part_selected_records(root, edits_doc, state):
    """Exact immutable selection/audio/voice references for an edition snapshot."""
    root = Path(root)
    paths = part_selected_audio_paths(root, edits_doc, state)
    result = []
    for uid, wav, meta_path in zip(sorted(state.part_selected_uids), paths[::2], paths[1::2]):
        e, meta = edits_doc["segs"][uid], _read_json(meta_path)
        result.append(dict(uid=uid, sid=e["sid"], take=e["take"], wav=str(wav.relative_to(root)),
                           metadata=str(meta_path.relative_to(root)), sha256=_file_hash(wav),
                           metadata_sha256=_file_hash(meta_path), role=meta.get("role"),
                           selected_role=e.get("role"), tech=meta.get("tech"), raw=meta.get("raw"),
                           source=meta.get("src"), basis=meta.get("basis"), vsig=meta.get("vsig")))
    return result


def read_protection(root, rows, check_audio=False, *, allow_preparing=False):
    """Read-only validation for Studio, workers, installer and status command."""
    root = Path(root)
    if not allow_preparing and (root / "work" / ".nox_prepare.lock").exists():
        raise ProtectionError("идёт подготовка книги (work/.nox_prepare.lock); дождитесь её завершения")
    if (root / "work" / ".nox_roles.lock").exists():
        raise ProtectionError("идёт или не завершена работа с голосами (work/.nox_roles.lock)")
    f, e = root / "studio" / "fixed_parts.json", root / "studio" / "edits.json"
    fixed = _read_json(f) if f.exists() else None
    edits = _read_json(e) if e.exists() else None
    state = validate_protection(rows, fixed, edits)
    if check_audio and (state.locked_uids or state.part_selected_uids):
        locked_audio_paths(root, edits, state)
    return state


def _groups(rows, label):
    groups, seen = [], set()
    for i, row in enumerate(rows):
        if "para" not in row or "chapter" not in row:
            raise ProtectionError(f"{label}: строка {i + 1} без para/chapter")
        key = (str(row["chapter"]), str(row["para"]))
        if not groups or key != groups[-1][0]:
            if key in seen:
                raise ProtectionError(f"{label}: абзац {key} встречается не подряд")
            seen.add(key)
            groups.append((key, []))
        groups[-1][1].append(row)
    return groups


def _paragraph_text(group):
    # Word/sentence splitting removes whitespace at chunk boundaries. Joining
    # with one space is conservative: a long token split differently fails,
    # instead of treating differently spaced words as identical.
    return " ".join(" ".join(r["text"].split()) for r in group).strip()


def _state_refs(st):
    refs = set(st["segs"])
    for p in st["parts"]:
        refs.add(p["last"])
    for item in st.get("ins", []):
        if item.get("after"):
            refs.add(item["after"])
    for key in ("cues", "rules"):
        values = st.get(key, [])
        if not isinstance(values, list):
            raise ProtectionError(f"edits.json: {key} должен быть списком")
        for item in values:
            if not isinstance(item, dict):
                raise ProtectionError(f"edits.json: повреждена запись {key}")
            for field in ("uid", "e_uid", "from_uid", "to_uid"):
                if item.get(field):
                    if not isinstance(item[field], str):
                        raise ProtectionError(f"edits.json: {key}.{field} должен быть строковым UID")
                    refs.add(item[field])
    approvals = st.get("stress_ok") or {}
    if not isinstance(approvals, dict) or not isinstance(approvals.get("places", {}), dict):
        raise ProtectionError("edits.json: неверный словарь подтверждённых ударений")
    refs.update(approvals.get("places", {}))
    return refs


def _check_retained_refs(old_rows, merged, st):
    """Keep every still-active edit/part/cue/insertion bound to proved content.

    No fuzzy remapping: occurrence-based UIDs may remain equal after inserting
    identical text. A reference outside the retained prefix therefore also
    needs a unique unchanged paragraph, or a completely unchanged paragraph
    sequence. Legacy orphan records are not treated as active references.
    """
    old_uids, new_uids = row_uids(old_rows), row_uids(merged)
    oi, ni = {u: i for i, u in enumerate(old_uids)}, {u: i for i, u in enumerate(new_uids)}
    og, ng = _groups(old_rows, "прежний CSV"), _groups(merged, "новый CSV")
    of, nf = [_paragraph_text(g) for _, g in og], [_paragraph_text(g) for _, g in ng]
    oc, nc = Counter(of), Counter(nf)
    op, np = {}, {}
    off = 0
    for k, (_, group) in enumerate(og):
        for u in old_uids[off:off + len(group)]: op[u] = k
        off += len(group)
    off = 0
    for k, (_, group) in enumerate(ng):
        for u in new_uids[off:off + len(group)]: np[u] = k
        off += len(group)
    inserted = {x["id"] for x in st.get("ins", [])}
    text_counts_old, text_counts_new = Counter(r["text"] for r in old_rows), Counter(r["text"] for r in merged)
    for uid in _state_refs(st) - inserted:
        if uid not in oi or uid not in ni:
            raise ProtectionError(f"правка/граница/звук {uid}: исходная реплика отсутствует после подготовки")
        text = old_rows[oi[uid]]["text"]
        if text_counts_old[text] != text_counts_new[text]:
            raise ProtectionError(f"правка/граница/звук {uid}: изменилось число одинаковых реплик; привязка неоднозначна")
        a, b = op[uid], np[uid]
        if of[a] != nf[b] or not (of == nf or (oc[of[a]] == nc[of[a]] == 1)):
            raise ProtectionError(f"правка/граница/звук {uid}: не подтверждён исходный абзац")


def merge_locked_prefix(old_rows, new_rows, fixed_doc=None, edits_doc=None, **_compat):
    if not isinstance(old_rows, list) or not isinstance(new_rows, list) or not new_rows:
        raise ProtectionError("новая разбивка пуста или имеет неверный тип")
    row_uids(new_rows)
    state = validate_protection(old_rows, fixed_doc, edits_doc)
    st = _edits(edits_doc)
    end = max(state.protected_indices, default=-1)
    # A locked insertion before the first CSV row also retains its following
    # paragraph; its position cannot be proven after replacing that paragraph.
    if end < 0 and state.locked_uids and old_rows:
        end = 0
    og, ng = _groups(old_rows, "прежний CSV"), _groups(new_rows, "новый CSV")
    count, retained = 0, 0
    if end >= 0:
        for _, group in og:
            retained += len(group)
            count += 1
            if retained > end:
                break
    of, nf = [_paragraph_text(g) for _, g in og], [_paragraph_text(g) for _, g in ng]
    if len(ng) < count or of[:count] != nf[:count]:
        raise ProtectionError("закреплённый префикс изменён, перемещён или содержит вставку; прежняя редакция оставлена")
    oc, nc = Counter(of), Counter(nf)
    if any(oc[f] != nc[f] for f in of[:count]):
        raise ProtectionError("изменилось число одинаковых защищённых абзацев; положение редакции неоднозначно")
    merged = []
    for k in range(count):
        meta = ng[k][1][0]
        for row in og[k][1]:
            kept = dict(row)
            for field in ("para", "chapter", "ch_title"):
                if field in meta:
                    kept[field] = meta[field]
            merged.append(kept)
    for _, group in ng[count:]:
        merged.extend(dict(r) for r in group)
    for i, row in enumerate(merged, 1):
        row["id"] = i
    if st["segs"] or st["parts"] or st.get("ins") or st.get("cues") or st.get("rules"):
        _check_retained_refs(old_rows, merged, st)
    # The same part boundaries and individual locks must still be valid.
    validate_protection(merged, fixed_doc, edits_doc)
    return FreezeMergeResult(merged, len(state.ranges), count, retained, len(merged) - retained, end)


def _bytes(path):
    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ProtectionError(f"ожидался файл: {path}")
    return path.read_bytes()


def _hash(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def preserve_pipeline_bundle(root, wav_path):
    """Copy the exact old WAV and its sidecars before a caller invalidates it.

    Originals are not changed here. The content-addressed location is also in
    edition manifests, so an old recording stays discoverable after repeated
    resegmentation or a voice change. Reuse is checked, never based on a name.
    """
    root, wav = Path(root).resolve(), Path(wav_path).resolve()
    try:
        wav.relative_to(root / "work" / "wav")
    except ValueError as exc:
        raise ProtectionError("архивировать можно только запись внутри work/wav") from exc
    if not wav.is_file():
        raise ProtectionError(f"нет исходной записи для сохранения: {wav}")
    digest = _file_hash(wav)
    target = root / "work" / "retained_audio" / digest
    target.mkdir(parents=True, exist_ok=True)
    records = {}
    paths = [p for p in wav.parent.glob(wav.stem + ".*") if p.is_file() and p.suffix not in (".lock", ".tmp")]
    for path in paths:
        data = path.read_bytes()
        expected = _hash(data)
        dest = target / path.name if path == wav else target / "sidecar_versions" / expected / path.name
        if dest.exists():
            if _file_hash(dest) != expected:
                raise ProtectionError(f"не совпал существующий архив записи: {dest}")
        if not dest.exists():
            _atomic_bytes(dest, data)
        if _file_hash(dest) != expected or _bytes(path) != data:
            raise ProtectionError(f"не прошла проверка архивирования {path}")
        records[path.name] = {"path": str(dest.relative_to(root)), "sha256": expected}
    if wav.name not in records or records[wav.name]["sha256"] != digest or _file_hash(wav) != digest:
        raise ProtectionError("WAV изменился во время архивирования")
    journal = target / ("bundle_" + uuid.uuid4().hex[:12] + ".json")
    _atomic_bytes(journal, json.dumps({"v": 1, "source": str(wav.relative_to(root)), "files": records},
                                     ensure_ascii=False, indent=2).encode("utf-8"))
    return target


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _csv_rows(data):
    if data is None:
        return []
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), delimiter=";")
        rows = list(reader)
        if not reader.fieldnames or "text" not in reader.fieldnames or any(None in r or None in r.values() for r in rows):
            raise ValueError("таблица оборвана или не содержит text")
        row_uids(rows)
        return rows
    except (ValueError, UnicodeError, csv.Error) as exc:
        raise ProtectionError(f"segments.csv не прочитан: {exc}") from exc


def _json_bytes(data, name):
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ProtectionError(f"{name} не прочитан: {exc}") from exc


_SNAPSHOT_PATHS = ("book.docx", "work/segments.csv", "work/roles.json", "studio/edits.json", "studio/fixed_parts.json",
                   "studio/sid_map.json", "studio/stress.json", "studio/home.id", "studio/sounds/index.json",
                   "work/parts.json", "work/assembly.json", "work/settings.json", "work/run_mode.json",
                   "work/accent_dict.csv", "work/speaker_overrides.tsv", "work/stress_fixes.tsv", "work/stress_words.txt")
_PREPARATIONS = {}


@contextlib.contextmanager
def preparation(root):
    """Share Studio's OS-level transaction lock for the complete preparation."""
    root = Path(root).resolve()
    if (root / "work" / ".nox_prepare.lock").exists():
        raise ProtectionError("незавершённая подготовка: work/.nox_prepare.lock; требуется проверка транзакции")
    if (root / "work" / ".nox_roles.lock").exists():
        raise ProtectionError("незавершённая операция голосов: work/.nox_roles.lock")
    with _edition_lock(root):
        with _preparation_locked(root) as tx:
            yield tx


@contextlib.contextmanager
def edition_write_lock(root):
    """Shared writer exclusion for a pipeline metadata/output publication."""
    root = Path(root).resolve()
    with _edition_lock(root):
        if (root / "work" / ".nox_prepare.lock").exists():
            raise ProtectionError("идёт или не завершена подготовка книги")
        if (root / "work" / ".nox_roles.lock").exists():
            raise ProtectionError("идёт или не завершена операция голосов")
        yield


@contextlib.contextmanager
def _edition_lock(root):
    root = Path(root).resolve()
    directory = root / "studio" / "transactions"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "operations.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ProtectionError("Studio меняет файлы книги; дождитесь завершения операции") from exc
            release = lambda: (lock.seek(0), msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ProtectionError("Studio меняет файлы книги; дождитесь завершения операции") from exc
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            yield
        finally:
            release()


@contextlib.contextmanager
def _preparation_locked(root):
    """Exclusive segment preparation, checked metadata snapshot, no audio moves.

    A surviving marker after a killed process is deliberate: inspect its PID
    and journal before explicit recovery. It is never silently treated as old.
    """
    root = Path(root).resolve()
    root.joinpath("work").mkdir(parents=True, exist_ok=True)
    marker = root / "work" / ".nox_prepare.lock"
    token = uuid.uuid4().hex
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ProtectionError("подготовка уже выполняется или оставила незавершённую транзакцию: work/.nox_prepare.lock") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(pid=os.getpid(), token=token, at=time.time()), f)
        f.flush()
        os.fsync(f.fileno())
    tx = None
    try:
        files = {rel: _bytes(root / rel) for rel in _SNAPSHOT_PATHS}
        rows = _csv_rows(files["work/segments.csv"])
        fixed = _json_bytes(files["studio/fixed_parts.json"], "fixed_parts.json")
        edits = _json_bytes(files["studio/edits.json"], "edits.json")
        state = validate_protection(rows, fixed, edits)
        audio = locked_audio_paths(root, edits, state) if state.locked_uids or state.part_selected_uids else []
        snapshot = root / "work" / "freeze_backups" / (time.strftime("%Y%m%d-%H%M%S") + "_" + token[:12])
        snapshot.mkdir(parents=True, exist_ok=False)
        manifest = {"v": 1, "revision": token[:12], "token": token, "pid": os.getpid(), "state": "prepared", "files": {},
                    "audio": {}, "part_selected": part_selected_records(root, edits, state),
                    "pipeline_audio": [], "catalog_ready": False}
        for rel, data in files.items():
            manifest["files"][rel] = _hash(data)
            if data is not None:
                _atomic_bytes(snapshot / rel, data)
                if _bytes(snapshot / rel) != data:
                    raise ProtectionError(f"не прошла проверка резервной копии {rel}")
        for path in audio:
            manifest["audio"][str(path.relative_to(root))] = _hash(_bytes(path))
        _atomic_bytes(snapshot / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        _atomic_bytes(marker, json.dumps(dict(pid=os.getpid(), token=token, at=time.time(),
                                             snapshot=str(snapshot.relative_to(root)))).encode("utf-8"))
        tx = dict(files=files, rows=rows, fixed=fixed, edits=edits, snapshot=snapshot, manifest=manifest, token=token)
        _PREPARATIONS[str(root)] = tx
        yield tx
    finally:
        _PREPARATIONS.pop(str(root), None)
        try:
            own = _read_json(marker).get("token") == token
        except ProtectionError:
            own = False
        if own and not (tx or {}).get("retain_marker"):
            # Atomic CSV writes either complete or leave the old file. Metadata
            # snapshots remain even on failure. An interrupted OS process never
            # reaches this finally, so its marker continues to protect the book.
            marker.unlink()


def merge_prepared_rows(root, new_rows):
    root = Path(root).resolve()
    tx = _PREPARATIONS.get(str(root))
    if tx is None:
        raise ProtectionError("merge_prepared_rows разрешён только внутри preparation(root)")
    result = merge_locked_prefix(tx["rows"], new_rows, tx["fixed"], tx["edits"])
    tx["result"] = result
    return result.rows


def commit_prepared_rows(root, new_rows, fieldnames, *, roles=None):
    """Merge and atomically publish CSV inside ``preparation(root)``.

    Existing selected audio is checked before publication and left byte-for-byte
    untouched. Backup includes metadata and audio hashes, not audio duplicates.
    """
    root = Path(root).resolve()
    tx = _PREPARATIONS.get(str(root))
    if tx is None:
        raise ProtectionError("commit_prepared_rows разрешён только внутри preparation(root)")
    result = tx.get("result")
    if result is None:
        result = merge_locked_prefix(tx["rows"], new_rows, tx["fixed"], tx["edits"])
    elif result.rows != new_rows:
        raise ProtectionError("проверенная разбивка была изменена после merge_prepared_rows")
    for rel, data in tx["files"].items():
        if _bytes(root / rel) != data:
            raise ProtectionError(f"во время подготовки изменён {rel}; публикация отменена")
    for rel, digest in tx["manifest"]["audio"].items():
        if _hash(_bytes(root / rel)) != digest:
            raise ProtectionError(f"во время подготовки изменён закреплённый дубль {rel}")
    for rel, digest in tx.get("pipeline_hashes", {}).items():
        if not (root / rel).is_file() or _file_hash(root / rel) != digest:
            raise ProtectionError(f"во время подготовки изменена исходная запись {rel}")
    if not fieldnames or any(any(k not in r for k in fieldnames) for r in result.rows):
        raise ProtectionError("новая разбивка не содержит обязательных столбцов")
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result.rows)
    data = buf.getvalue().encode("utf-8")
    # Check serialization before replacing anything.
    if row_uids(_csv_rows(data)) != row_uids(result.rows):
        raise ProtectionError("проверка сериализации новой разбивки не прошла")
    updates = {}
    if roles is not None:
        if not isinstance(roles, dict) or not isinstance(roles.get("roles"), dict):
            raise ProtectionError("неверная структура новых roles.json")
        updates["work/roles.json"] = json.dumps(roles, ensure_ascii=False, indent=2).encode("utf-8")
    source_files = tx.get("source_files", [])
    for path, original in source_files:
        if _bytes(path) != original:
            raise ProtectionError(f"рукопись {path} изменилась во время подготовки")
    if len(source_files) == 1:
        updates["book.docx"] = source_files[0][1]
    updates["work/segments.csv"] = data  # publish the CSV last
    tx["manifest"].update(state="publishing", new_files={r: _hash(d) for r, d in updates.items()})
    manifest_path = tx["snapshot"] / "manifest.json"
    def journal():
        _atomic_bytes(manifest_path, json.dumps(tx["manifest"], ensure_ascii=False, indent=2).encode("utf-8"))
    journal()
    written = []
    try:
        for rel, payload in updates.items():
            # Recheck immediately before each atomic replacement as well.
            if _bytes(root / rel) != tx["files"][rel]:
                raise ProtectionError(f"параллельное изменение {rel}; публикация отменена")
            _atomic_bytes(root / rel, payload)
            written.append(rel)
        tx["manifest"]["state"] = "committed"
        journal()
    except BaseException:
        failed = []
        for rel in reversed(written):
            try:
                if _bytes(root / rel) != updates[rel]:
                    raise ProtectionError(f"параллельное изменение опубликованного {rel}")
                previous = tx["files"][rel]
                if previous is None:
                    (root / rel).unlink()
                else:
                    _atomic_bytes(root / rel, previous)
            except Exception as exc:
                failed.append(f"{rel}: {exc}")
        tx["manifest"]["state"] = "recovery_required" if failed else "rolled_back"
        tx["manifest"]["rollback_errors"] = failed
        tx["retain_marker"] = bool(failed)
        try:
            journal()
        except Exception:
            tx["retain_marker"] = True
        if failed:
            raise ProtectionError(f"нужна проверка восстановления {tx['snapshot']}: {'; '.join(failed)}")
        raise
    print(f"защита редакции: сохранено {result.preserved_segments} исходных реплик; снимок {tx['snapshot']}", flush=True)
    return result.rows


def guard_segment(function):
    """Decorator for the generated Stage 21 ``segment`` entry point."""
    def protected_segment(*args, **kwargs):
        root = Path(function.__globals__["HERE"])
        with preparation(root) as tx:
            paths = args[0] if args else kwargs.get("docx_paths", [])
            tx["source_files"] = [(Path(p).resolve(), _bytes(p)) for p in paths]
            if any(data is None for _, data in tx["source_files"]):
                raise ProtectionError("исходная рукопись отсутствует")
            _record_pipeline_catalog(root.resolve(), tx, function.__globals__)
            return function(*args, **kwargs)
    protected_segment.__name__ = function.__name__
    protected_segment.__doc__ = function.__doc__
    return protected_segment


def guard_metadata(function):
    """Writer guard for generated pipeline metadata/check operations."""
    def checked(*args, **kwargs):
        root = Path(function.__globals__["HERE"])
        with edition_write_lock(root):
            read_protection(root, _csv_rows(_bytes(root / "work" / "segments.csv")))
            return function(*args, **kwargs)
    checked.__name__ = function.__name__
    checked.__doc__ = function.__doc__
    return checked


def _record_pipeline_catalog(root, tx, runtime):
    """Capture provenance using the real runtime's wav_for, never reverse hashes."""
    rows = tx["rows"]
    roles = _json_bytes(tx["files"]["work/roles.json"], "roles.json")
    manifest, cache = tx["manifest"], {}
    tx["pipeline_hashes"] = {}
    if rows and roles is None:
        raise ProtectionError("прежняя разбивка существует, но roles.json отсутствует; старые записи нельзя сопоставить")
    for index, (uid, row) in enumerate(zip(row_uids(rows), rows)):
        wav = Path(runtime["wav_for"](row, roles)).resolve()
        try:
            rel = wav.relative_to(root / "work" / "wav")
        except ValueError as exc:
            raise ProtectionError("wav_for вернул запись за пределами work/wav") from exc
        if not wav.is_file():
            continue
        if wav not in cache:
            digest, sidecars = _file_hash(wav), {}
            vault = root / "work" / "retained_audio" / digest
            for suffix in (".txt", ".meta", ".ok", ".gaps"):
                side = wav.with_suffix(suffix)
                if side.is_file():
                    sd = _file_hash(side)
                    sr = str(side.relative_to(root))
                    sidecars[suffix[1:]] = dict(path=sr, sha256=sd,
                                              retained_path=str((vault / "sidecar_versions" / sd / side.name).relative_to(root)))
                    tx["pipeline_hashes"][sr] = sd
            cache[wav] = dict(wav=str(wav.relative_to(root)), sha256=digest, size=wav.stat().st_size,
                              retained_wav=str((vault / wav.name).relative_to(root)), sidecars=sidecars)
            tx["pipeline_hashes"][str(wav.relative_to(root))] = digest
        record = dict(cache[wav], uid=uid, index=index, id=str(row.get("id", index + 1)),
                      role=runtime["resolve_role"](row["speaker"], roles), text=row["text"], wav_rule="runtime.wav_for")
        manifest["pipeline_audio"].append(record)
    manifest["catalog_ready"] = True
    _atomic_bytes(tx["snapshot"] / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))


def _pid_alive(pid):
    maximum = 0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF
    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= maximum:
        raise ProtectionError("неверный PID незавершённой операции")
    if os.name == "nt":
        # os.kill(pid, 0) is NOT a harmless probe on Windows. Request only
        # read-only process information; never acquire PROCESS_TERMINATE.
        import ctypes
        try:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            api.OpenProcess.restype = ctypes.c_void_p
            api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            api.GetExitCodeProcess.restype = ctypes.c_int
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            api.CloseHandle.restype = ctypes.c_int
            handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # A validated positive DWORD PID with ERROR_INVALID_PARAMETER
                # has no process object. Access denied and every uncertain
                # failure keep recovery blocked.
                return ctypes.get_last_error() != 87
            alive = True
            try:
                code = ctypes.c_uint32()
                if api.GetExitCodeProcess(handle, ctypes.byref(code)):
                    alive = code.value == 259  # STILL_ACTIVE; ambiguity stays blocked
            finally:
                if not api.CloseHandle(handle):
                    alive = True
            return alive
        except (OSError, AttributeError, TypeError, ValueError):
            return True  # API unavailable/failed: never infer that the writer died
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # uncertain is not proof that a writer has stopped
    return True


def recover_preparation(root):
    """Explicit rollback of an interrupted prep, with backup/current hash checks.

    Nothing runs this implicitly during load/install. A live owner, unknown
    file content, changed audio or damaged backup stops recovery unchanged.
    """
    root = Path(root).resolve()
    marker = root / "work" / ".nox_prepare.lock"
    with _edition_lock(root):
        info = _read_json(marker)
        if not isinstance(info, dict) or not isinstance(info.get("token"), str):
            raise ProtectionError("повреждён marker подготовки")
        if _pid_alive(info.get("pid")):
            raise ProtectionError("процесс подготовки ещё существует; восстановление запрещено")
        rel = info.get("snapshot")
        if not isinstance(rel, str):
            raise ProtectionError("подготовка не оставила проверяемый snapshot; автоматическое удаление marker запрещено")
        snapshot = (root / rel).resolve()
        if snapshot.parent != root / "work" / "freeze_backups":
            raise ProtectionError("неверный путь snapshot подготовки")
        manifest = _read_json(snapshot / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("token") != info["token"]:
            raise ProtectionError("snapshot не соответствует marker подготовки")
        before, after = manifest.get("files"), manifest.get("new_files", {})
        if not isinstance(before, dict) or set(before) != set(_SNAPSHOT_PATHS) or not isinstance(after, dict):
            raise ProtectionError("неполный журнал подготовки")
        if set(after) - {"book.docx", "work/roles.json", "work/segments.csv"}:
            raise ProtectionError("журнал содержит неизвестные изменения")
        original = {}
        for rel, expected in before.items():
            original[rel] = _bytes(snapshot / rel)
            if _hash(original[rel]) != expected:
                raise ProtectionError(f"повреждена резервная копия {rel}")
            current = _hash(_bytes(root / rel))
            if current != expected and (rel not in after or current != after[rel]):
                raise ProtectionError(f"{rel} изменён после подготовки; восстановление без выбора запрещено")
        for rel, expected in manifest.get("audio", {}).items():
            path = (root / rel).resolve()
            if root not in path.parents or _hash(_bytes(path)) != expected:
                raise ProtectionError(f"закреплённый звук изменён или потерян: {rel}")
        if manifest.get("state") == "committed":
            if any(_hash(_bytes(root / rel)) != expected for rel, expected in after.items()):
                raise ProtectionError("завершённая подготовка имеет неполный результат")
            outcome = "completed"
        elif manifest.get("state") in ("prepared", "publishing", "rolled_back", "recovery_required"):
            for rel in reversed(tuple(after)):
                current = _hash(_bytes(root / rel))
                if current == before[rel]:
                    continue
                if current != after[rel]:
                    raise ProtectionError(f"параллельное изменение при восстановлении: {rel}")
                if original[rel] is None:
                    (root / rel).unlink()
                else:
                    _atomic_bytes(root / rel, original[rel])
            outcome = "rolled_back"
        else:
            raise ProtectionError("неизвестная стадия подготовки")
        read_protection(root, _csv_rows(_bytes(root / "work/segments.csv")), check_audio=True, allow_preparing=True)
        manifest["state"] = "committed" if outcome == "completed" else "rolled_back"
        manifest["recovered_at"] = time.time()
        _atomic_bytes(snapshot / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        if _read_json(marker).get("token") != info["token"]:
            raise ProtectionError("marker изменился во время восстановления")
        marker.unlink()
        return dict(state=outcome, snapshot=str(snapshot))


if __name__ == "__main__":
    import argparse
    cli = argparse.ArgumentParser(description="Проверяемое восстановление прерванной подготовки NOX")
    cli.add_argument("--root", required=True)
    cli.add_argument("--recover", action="store_true", required=True)
    args = cli.parse_args()
    try:
        print(json.dumps(recover_preparation(args.root), ensure_ascii=False))
    except ProtectionError as exc:
        cli.exit(4, f"Восстановление остановлено: {exc}\n")
