"""Checked, recoverable quarantine for unused voice samples; no audio deletion."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from nox_freeze import (ProtectionError, edition_write_lock, read_protection, part_selected_records,
                        _atomic_bytes, _edition_lock, _pid_alive)


def _bytes(path):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProtectionError(f"образец или документ не является обычным файлом: {path.name}")
    return path.read_bytes() if path.exists() else None


def _sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _hash(path):
    return _sha(_bytes(path))


def _json(path, *, missing=None):
    raw = _bytes(path)
    if raw is None:
        return copy.deepcopy(missing), raw
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(doc, dict):
            raise ValueError("ожидался объект")
        return doc, raw
    except (ValueError, UnicodeError) as exc:
        raise ProtectionError(f"повреждён документ голосов: {Path(path).name}") from exc


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _name(value):
    if not isinstance(value, str) or not value or value in (".", "..") or any(c in value for c in '/\\\0:'):
        raise ProtectionError("неверное имя образца в метаданных голосов")
    return value


def _inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ProtectionError("неверный путь в журнале голосов")
    p = root / relative
    resolved = p.resolve()
    if root not in resolved.parents or resolved != p.absolute():
        raise ProtectionError("путь образца выходит за каталог или проходит через ссылку")
    return p


def _roles(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("roles"), dict) or any(not isinstance(c, dict) for c in doc["roles"].values()):
        raise ProtectionError("неверная структура roles.json")
    return doc


def _history_roles(root, guards):
    """Only metadata whose content hash is supplied by its actual snapshot."""
    found = []
    for manifest in sorted((root / "work/freeze_backups").glob("*/manifest.json")):
        doc, raw = _json(manifest)
        if doc.get("state") not in ("prepared", "committed", "rolled_back"):
            raise ProtectionError("снимок подготовки требует восстановления; очистка голосов остановлена")
        files = doc.get("files")
        if not isinstance(files, dict):
            raise ProtectionError("повреждён список файлов снимка подготовки")
        if "work/roles.json" not in files:
            continue
        p = manifest.parent / "work/roles.json"
        roles, rb = _json(p)
        if rb is None or _sha(rb) != files["work/roles.json"]:
            raise ProtectionError("не подтверждён roles.json прежнего снимка подготовки")
        for source, data in ((manifest, raw), (p, rb)):
            _inside(root, source.relative_to(root).as_posix())
            guards[source.relative_to(root).as_posix()] = _sha(data)
        found.append(_roles(roles))
    for p in sorted((root / "work/voice_history").glob("roles_*.json")):
        roles, raw = _json(p)
        if p.name != "roles_" + str(_sha(raw)) + ".json":
            raise ProtectionError("не подтверждён сохранённый снимок ролей")
        _inside(root, p.relative_to(root).as_posix())
        guards[p.relative_to(root).as_posix()] = _sha(raw)
        found.append(_roles(roles))
    return found


def _plan(n, bank, catalog):
    root = Path(n.HERE).resolve()
    if Path(n.ROLES_PATH).resolve() != root / "work/roles.json" or Path(n.VOICES).resolve() != root / "work/voices":
        raise ProtectionError("не подтверждены пути голосов книги")
    voices_dir = root / "work/voices"
    if bank == voices_dir or bank in voices_dir.parents or voices_dir in bank.parents:
        raise ProtectionError("банк голосов пересекается с каталогом образцов книги")
    roles, raw_roles = _json(root / "work/roles.json")
    _roles(roles)
    rows = n.load_rows() if Path(n.CSV_PATH).exists() else []
    state = read_protection(root, rows)
    guards = {rel: _hash(_inside(root, rel)) for rel in ("work/segments.csv", "studio/edits.json", "studio/fixed_parts.json")}
    history = _history_roles(root, guards)
    keep = set()
    historical_bank = set()
    for doc in [roles] + history:
        stems = n.voice_stems(doc)
        for role, cfg in doc["roles"].items():
            for p in n.voice_files(role, stems):
                if Path(p).parent.resolve() != root / "work/voices":
                    raise ProtectionError("неверный путь образца роли")
                keep.add(p.name.casefold())
                if Path(p).exists():
                    guards[Path(p).relative_to(root).as_posix()] = _hash(p)
            if doc is not roles and (cfg.get("voice") or {}).get("bank"):
                historical_bank.add(_name(cfg["voice"]["bank"]))
    # Names referenced by archived sample bundles remain explicitly protected.
    for manifest in sorted((root / "work/voice_history/samples").glob("*/manifest.json")):
        md, raw = _json(manifest)
        if not isinstance(md.get("files"), dict):
            raise ProtectionError("повреждён снимок образцов голоса")
        guards[manifest.relative_to(root).as_posix()] = _sha(raw)
        for name, record in md["files"].items():
            _name(name)
            if not isinstance(record, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) or _hash(manifest.parent / name) != record["sha256"]:
                raise ProtectionError("не подтверждён сохранённый образец голоса")
            source = Path(str(record.get("source", "")))
            if source.parent.resolve() == root / "work/voices":
                keep.add(source.name.casefold())
                if source.exists():
                    guards[source.relative_to(root).as_posix()] = _hash(source)
    edits, _ = _json(root / "studio/edits.json", missing={})
    protected = set()
    def protect(role):
        if not isinstance(role, str) or not role:
            return
        protected.add(role)
        role = roles.get("aliases", {}).get(role, role)
        if role.startswith("UNKNOWN") or role not in roles["roles"]:
            role = roles.get("unknown_role", "narrator")
        seen = set()
        while role:
            if role in seen or role not in roles["roles"]:
                raise ProtectionError("не подтверждена цепочка закреплённого голоса")
            seen.add(role)
            protected.add(role)
            role = roles["roles"][role].get("same_as")
    for i in state.protected_indices:
        protect(rows[i]["speaker"])
    for uid in state.locked_uids:
        protect(((edits.get("segs", {}).get(uid) or {}).get("locked") or {}).get("role"))
    for selected in part_selected_records(root, edits, state):
        protect(selected.get("selected_role"))
        protect(selected.get("role"))
    for item in edits.get("ins", []):
        if item.get("id") in state.locked_uids | state.part_selected_uids:
            protect(item.get("role"))
    historical_bank.update(_name((roles["roles"][r].get("voice") or {})["bank"])
                           for r in protected if r in roles["roles"] and (roles["roles"][r].get("voice") or {}).get("bank"))
    bankdoc, raw_bank = _json(bank / "bank.json", missing={"ver": 1, "voices": {}})
    if not isinstance(bankdoc.get("voices"), dict) or any(not isinstance(v, dict) for v in bankdoc["voices"].values()):
        raise ProtectionError("неверная структура bank.json")
    for vid in bankdoc["voices"]:
        _name(vid)
    vdir = root / "work/voices"
    candidates = sorted(p for p in vdir.iterdir() if p.suffix.lower() in (".wav", ".txt") and p.name.casefold() not in keep) if vdir.exists() else []
    for p in candidates:
        _inside(root, p.relative_to(root).as_posix())
        _bytes(p)
    dead = [(vid, rec) for vid, rec in bankdoc["voices"].items() if rec.get("kind") in ("book", "custom")
            and not (bank / (vid + ".wav")).exists() and vid not in historical_bank]
    known = {str(name).casefold() for name in ({c[0] for c in catalog} | set(bankdoc["voices"]) | historical_bank)}
    stray = sorted(p for p in bank.glob("*.wav") if p.stem.casefold() not in known)
    bankfiles = []
    for p in stray:
        for ext in (".wav", ".txt", ".json"):
            q = p.with_suffix(ext)
            if q.exists():
                _inside(bank, q.relative_to(bank).as_posix())
                _bytes(q)
                bankfiles.append(q)
    # An orphan WAV's optional JSON is part of the same archive bundle.
    for p in list(candidates):
        if p.suffix.lower() == ".wav" and p.with_suffix(".json").exists():
            q = p.with_suffix(".json")
            _inside(root, q.relative_to(root).as_posix())
            _bytes(q)
            candidates.append(q)
    candidates = sorted(set(candidates))
    dead_ids = {vid for vid, _ in dead}
    unbound = [r for r, cfg in roles["roles"].items() if r not in protected and (cfg.get("voice") or {}).get("bank") in dead_ids]
    prev_gone = [r for r, cfg in roles["roles"].items() if r not in protected and (cfg.get("voice_prev") or {}).get("file")
                 and not (root / "work/voices_old" / (_name(cfg["voice_prev"]["file"]) + ".wav")).exists()]
    new_roles, new_bank = copy.deepcopy(roles), copy.deepcopy(bankdoc)
    for vid in dead_ids:
        new_bank["voices"].pop(vid)
    for role in unbound:
        v = new_roles["roles"][role]["voice"]
        v.pop("bank", None)
        if not v.get("name"):
            new_roles["roles"][role].pop("voice", None)
    for role in prev_gone:
        new_roles["roles"][role].pop("voice_prev", None)
    rep = dict(applied=False, book=dict(orphans=[p.name for p in candidates], kb=round(sum(p.stat().st_size for p in candidates) / 1024),
                                     to=str(root / "work/voices_old/bez_roli")),
               bank=dict(dead=[dict(id=vid, name=rec.get("name") or vid) for vid, rec in dead], stray=[p.name for p in stray],
                         to=str(bank / "bez_zapisi")), roles=dict(unbound=unbound, prev_gone=prev_gone),
               protection=dict(snapshots=len(history), names=len(keep), roles=sorted(protected)))
    documents = []
    for scope, rel, before, after in (("book", "work/roles.json", raw_roles, _encode(new_roles) if new_roles != roles else raw_roles),
                                     ("bank", "bank.json", raw_bank, _encode(new_bank) if new_bank != bankdoc else raw_bank)):
        documents.append(dict(scope=scope, path=rel, before_bytes=before, after_bytes=after))
    return root, rep, candidates, bankfiles, documents, guards


def _seal(info):
    return _sha(_encode({k: info[k] for k in ("v", "tool", "token", "root", "bank", "moves", "documents", "guards")}))


def _paths(info, root):
    if info.get("v") != 1 or info.get("tool") != "voice_tidy" or not re.fullmatch(r"[0-9a-f]{32}", str(info.get("token", ""))):
        raise ProtectionError("неверный журнал очистки голосов")
    if info.get("root") != str(root) or info.get("plan_sha256") != _seal(info):
        raise ProtectionError("не подтверждены книга или контрольная сумма журнала голосов")
    bank = Path(info["bank"])
    if not bank.is_absolute() or bank.resolve() != bank:
        raise ProtectionError("неверный путь банка в журнале голосов")
    homes = {"book": root, "bank": bank}
    token = info["token"]
    moves = []
    for item in info["moves"]:
        scope = item.get("scope")
        if scope not in homes:
            raise ProtectionError("неверная область образца")
        src, dst = (_inside(homes[scope], item[key]) for key in ("source", "target"))
        src_parent = root / "work/voices" if scope == "book" else bank
        dst_parent = root / "work/voices_old/bez_roli" / token if scope == "book" else bank / "bez_zapisi" / token
        if src.parent != src_parent or dst.parent != dst_parent or dst.name != src.name or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise ProtectionError("неверная пара путей образца")
        moves.append((src, dst, item["sha256"]))
    docs = []
    folder = root / "work/voice_history/tidy" / token
    for item in info["documents"]:
        scope, relative = item.get("scope"), item.get("path")
        if (scope, relative) not in (("book", "work/roles.json"), ("bank", "bank.json")):
            raise ProtectionError("неверный документ в журнале голосов")
        p = _inside(homes[scope], relative)
        before = _bytes(folder / (scope + ".before")) if item["before"] is not None else None
        after = _bytes(folder / (scope + ".after")) if item["after"] is not None else None
        if _sha(before) != item["before"] or _sha(after) != item["after"]:
            raise ProtectionError("повреждена резервная копия метаданных голосов")
        docs.append((p, before, after))
    return moves, docs


def _check(info, root, phase=None):
    moves, docs = _paths(info, root)
    for relative, sha in info["guards"].items():
        if _hash(_inside(root, relative)) != sha:
            raise ProtectionError(f"данные книги изменились во время очистки голосов: {relative}")
    for src, dst, sha in moves:
        pair = (_hash(src), _hash(dst))
        choices = {"before": [(sha, None)], "after": [(None, sha)]}.get(phase, [(sha, None), (None, sha)])
        if pair not in choices:
            raise ProtectionError(f"образец изменён вне операции или неоднозначен: {src.name}")
    for p, before, after in docs:
        expected = [_sha(before)] if phase == "before" else [_sha(after)] if phase == "after" else [_sha(before), _sha(after)]
        if _hash(p) not in expected:
            raise ProtectionError(f"метаданные изменены вне операции: {p.name}")
    return moves, docs


def _move(src, dst):
    if dst.exists():
        raise ProtectionError(f"место сохранения образца уже занято: {dst.name}")
    os.rename(src, dst)


def _rollback(info, root):
    moves, docs = _check(info, root)
    for src, dst, sha in reversed(moves):
        if not src.exists():
            _move(dst, src)
    for p, before, after in reversed(docs):
        if _hash(p) == _sha(before):
            continue
        if before is None:
            # Only a file created by this transaction can satisfy the check above.
            p.unlink()
        else:
            _atomic_bytes(p, before)
    _check(info, root, "before")


def tidy_voices(n, bank, catalog, *, apply=False):
    root = Path(n.HERE).resolve()
    bank = Path(bank).resolve()
    lock = edition_write_lock(root) if apply else contextlib.nullcontext()
    with lock:
        root, rep, sources, bankfiles, documents, guards = _plan(n, bank, catalog)
        if not apply:
            return rep
        rep["applied"] = True
        if not sources and not bankfiles and all(d["before_bytes"] == d["after_bytes"] for d in documents):
            return rep
        token = uuid.uuid4().hex
        folder = root / "work/voice_history/tidy" / token
        folder.mkdir(parents=True, exist_ok=False)
        moves = []
        for scope, home, files, parent in (("book", root, sources, root / "work/voices_old/bez_roli"),
                                          ("bank", bank, bankfiles, bank / "bez_zapisi")):
            if files:
                dest = parent / token
                dest.mkdir(parents=True, exist_ok=False)
                for p in files:
                    moves.append(dict(scope=scope, source=p.relative_to(home).as_posix(), target=(dest / p.name).relative_to(home).as_posix(),
                                      sha256=_hash(p), size=p.stat().st_size))
        metadata = []
        for d in documents:
            for key in ("before", "after"):
                if d[key + "_bytes"] is not None:
                    _atomic_bytes(folder / (d["scope"] + "." + key), d[key + "_bytes"])
            metadata.append(dict(scope=d["scope"], path=d["path"], before=_sha(d["before_bytes"]), after=_sha(d["after_bytes"])))
        info = dict(v=1, tool="voice_tidy", token=token, root=str(root), bank=str(bank), pid=os.getpid(), at=time.time(),
                    state="prepared", moves=moves, documents=metadata, guards=guards)
        info["plan_sha256"] = _seal(info)
        journal = folder / "manifest.json"
        marker = root / "work/.nox_roles.lock"
        _atomic_bytes(journal, _encode(info))
        marker_data = _encode(dict(v=1, tool="voice_tidy", token=token, pid=os.getpid(), bank=str(bank),
                                  journal=journal.relative_to(root).as_posix()))
        with marker.open("xb") as f:
            f.write(marker_data)
            f.flush()
            os.fsync(f.fileno())
        try:
            move_paths, docs = _check(info, root, "before")
            for src, dst, sha in move_paths:
                _move(src, dst)
                if _hash(dst) != sha:
                    raise ProtectionError("образец изменился при перемещении")
            for p, before, after in docs:
                if _hash(p) != _sha(before):
                    raise ProtectionError("метаданные голосов изменились во время операции")
                if before != after:
                    _atomic_bytes(p, after)
            _check(info, root, "after")
            info["state"] = "committed"
            _atomic_bytes(journal, _encode(info))
            marker.unlink()
        except Exception as exc:
            try:
                _rollback(info, root)
                info["state"] = "rolled_back"
                _atomic_bytes(journal, _encode(info))
                marker.unlink()
            except Exception as rollback_error:
                info["state"] = "recovery_required"
                _atomic_bytes(journal, _encode(info))
                raise ProtectionError("очистка голосов остановлена; журнал сохранён, требуется явное восстановление") from rollback_error
            raise exc
        rep["journal"] = str(journal)
        rep["book"]["to"] = str(root / "work/voices_old/bez_roli" / token)
        rep["bank"]["to"] = str(bank / "bez_zapisi" / token)
        return rep


def recover_tidy(root):
    """Explicit recovery only; a live owner or unknown content always blocks it."""
    root = Path(root).resolve()
    marker = root / "work/.nox_roles.lock"
    with _edition_lock(root):
        if (root / "work/.nox_prepare.lock").exists():
            raise ProtectionError("сначала требуется восстановить подготовку книги")
        md, _ = _json(marker)
        token = str(md.get("token", ""))
        if md.get("tool") != "voice_tidy" or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ProtectionError("маркер не относится к очистке голосов")
        if _pid_alive(md.get("pid")):
            raise ProtectionError("процесс очистки голосов ещё существует; восстановление запрещено")
        journal = root / "work/voice_history/tidy" / token / "manifest.json"
        if md.get("journal") != journal.relative_to(root).as_posix():
            raise ProtectionError("неверная ссылка на журнал голосов")
        info, _ = _json(journal)
        if info.get("token") != token or info.get("bank") != md.get("bank"):
            raise ProtectionError("маркер не соответствует журналу голосов")
        if info.get("state") == "committed":
            _check(info, root, "after")
        elif info.get("state") in ("prepared", "rolled_back", "recovery_required"):
            _rollback(info, root)
            info["state"] = "rolled_back"
        else:
            raise ProtectionError("неизвестное состояние операции голосов")
        info["recovered_at"] = time.time()
        _atomic_bytes(journal, _encode(info))
        marker.unlink()
    return "очистка голосов восстановлена по проверенному журналу"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--recover", action="store_true", required=True)
    args = parser.parse_args()
    print(recover_tidy(args.root))


if __name__ == "__main__":
    main()
