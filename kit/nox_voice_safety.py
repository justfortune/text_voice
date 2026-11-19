"""Protection and versioning for automatic voice/profile maintenance."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import uuid

from nox_freeze import (ProtectionError, edition_write_lock, read_protection,
                        preserve_pipeline_bundle, _atomic_bytes, _file_hash,
                        _pid_alive, _csv_rows, validate_protection, locked_audio_paths, _edition_lock,
                        part_selected_audio_paths, part_selected_records)


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _current(path):
    return path.read_bytes() if path.is_file() else None


def _safe_asset(root, relative):
    path = (root / relative).resolve()
    allowed = path == root / "work/roles.json" or path.parent in (root / "work/voices", root / "work/wav")
    if not isinstance(relative, str) or not allowed or path.is_symlink():
        raise ProtectionError("неверный путь в журнале смены голосов")
    return path


def _restore_roles_transaction(root, info):
    """Validate every preimage/current hash before restoring any file."""
    if info.get("v") != 2 or not isinstance(info.get("changes"), dict) or not isinstance(info.get("guards"), dict):
        raise ProtectionError("нет полного журнала операции голосов; автоматическое восстановление невозможно")
    for rel, expected in info["guards"].items():
        path = (root / rel).resolve()
        if root not in path.parents or _digest(_current(path)) != expected:
            raise ProtectionError(f"защищённые данные изменились после операции голосов: {rel}")
    restore = []
    for rel, record in info["changes"].items():
        path = _safe_asset(root, rel)
        before = record.get("before")
        original = None
        if before is not None:
            backup = (root / record["backup"]).resolve()
            if not any(parent in backup.parents for parent in (root / "work/voice_history", root / "work/retained_audio")):
                raise ProtectionError("неверный путь резервной копии голоса")
            original = _current(backup)
            if _digest(original) != before:
                raise ProtectionError(f"не подтверждена резервная копия {rel}")
        if _digest(_current(path)) not in [before] + record.get("after", []):
            raise ProtectionError(f"файл изменён вне операции голосов: {rel}")
        restore.append((path, original))
    for path, original in reversed(restore):
        if original is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_bytes(path, original)
    rows = _csv_rows(_current(root / "work/segments.csv"))
    def doc(name):
        raw = _current(root / name)
        return json.loads(raw.decode("utf-8-sig")) if raw is not None else None
    edits = doc("studio/edits.json")
    state = validate_protection(rows, doc("studio/fixed_parts.json"), edits)
    if state.locked_uids or state.part_selected_uids:
        locked_audio_paths(root, edits, state)


def recover_roles(root):
    """Explicit rollback after a dead owner; never run automatically."""
    root = Path(root).resolve()
    marker = root / "work/.nox_roles.lock"
    if marker.is_file():
        marker_info = json.loads(marker.read_text(encoding="utf-8"))
        if marker_info.get("tool") == "voice_tidy":
            from nox_voice_tidy import recover_tidy
            return recover_tidy(root)
    with _edition_lock(root):
        if (root / "work/.nox_prepare.lock").exists():
            raise ProtectionError("сначала требуется проверить незавершённую подготовку книги")
        info = json.loads(marker.read_text(encoding="utf-8"))
        if info.get("v") != 2 or not re.fullmatch(r"[0-9a-f]{32}", str(info.get("token", ""))):
            raise ProtectionError("повреждён журнал операции голосов")
        if _pid_alive(info.get("pid")):
            raise ProtectionError("процесс смены голосов ещё существует; восстановление запрещено")
        if info.get("state") == "committed":
            for rel, expected in info.get("guards", {}).items():
                path = (root / rel).resolve()
                if root not in path.parents or _digest(_current(path)) != expected:
                    raise ProtectionError(f"защищённые данные изменились после операции голосов: {rel}")
            for rel, record in info.get("changes", {}).items():
                if _digest(_current(_safe_asset(root, rel))) != record["after"][-1]:
                    raise ProtectionError(f"файл изменён после завершения операции голосов: {rel}")
        else:
            _restore_roles_transaction(root, info)
        info["state"] = "recovered"
        _atomic_bytes(root / "work/voice_history/transactions" / (info["token"] + ".json"),
                      json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"))
        marker.unlink()
    return "операция голосов восстановлена по проверенным копиям"


class RoleSafety:
    def __init__(self, runtime, readonly=False):
        self.n = runtime
        self.root = Path(runtime.HERE).resolve()
        self.readonly = readonly
        self.context = None
        self.token = uuid.uuid4().hex
        self.marker = self.root / "work" / ".nox_roles.lock"

    def __enter__(self):
        if not self.readonly:
            self.context = edition_write_lock(self.root)
            self.context.__enter__()
        try:
            if self.marker.exists():
                raise ProtectionError("не завершена операция голосов: work/.nox_roles.lock; проверьте work/voice_history")
            self.initial_bytes = self.n.ROLES_PATH.read_bytes()
            self.original = json.loads(self.initial_bytes.decode("utf-8-sig"))
            self.rows = self.n.load_rows() if self.n.CSV_PATH.exists() else []
            self.state = read_protection(self.root, self.rows)
            self.protected = set()
            aliases = self.original.get("aliases", {})
            def add(role):
                if not isinstance(role, str) or not role:
                    return
                self.protected.add(role)
                current = aliases.get(role, role)
                if current.startswith("UNKNOWN") or current not in self.original["roles"]:
                    current = self.original.get("unknown_role", "narrator")
                seen = set()
                while current:
                    if current in seen or current not in self.original["roles"]:
                        raise ProtectionError(f"не подтверждена цепочка голоса закреплённой роли {role}")
                    seen.add(current)
                    self.protected.add(current)
                    current = self.original["roles"][current].get("same_as")
            for i in self.state.protected_indices:
                add(self.rows[i]["speaker"])
            ep = self.root / "studio/edits.json"
            edits = json.loads(ep.read_text(encoding="utf-8-sig")) if ep.exists() else {}
            for uid in self.state.locked_uids:
                add((edits.get("segs", {}).get(uid, {}).get("locked") or {}).get("role"))
            selected_audio = part_selected_audio_paths(self.root, edits, self.state)
            for record in part_selected_records(self.root, edits, self.state):
                add(record.get("selected_role"))
                add(record.get("role"))
            for ins in edits.get("ins", []):
                if ins["id"] in self.state.locked_uids or ins["id"] in self.state.part_selected_uids:
                    add(ins.get("role"))
            stems = self.n.voice_stems(self.original)
            self.samples = {role: self.n.voice_files(role, stems) for role in self.original["roles"]}
            self.bindings = [(i, r, self.n.resolve_role(r["speaker"], self.original), self.n.wav_for(r, self.original))
                             for i, r in enumerate(self.rows)]
            self.protected_wavs = {p for i, r, role, p in self.bindings if i in self.state.protected_indices}
            if not self.readonly:
                paths = [self.root / p for p in ("work/segments.csv", "studio/edits.json", "studio/fixed_parts.json")]
                paths += [Path(p) for role in self.protected for p in self.samples.get(role, ())]
                paths += selected_audio
                self.journal = {"v": 2, "pid": os.getpid(), "token": self.token, "state": "active", "changes": {},
                                "guards": {str(p.relative_to(self.root)): _digest(_current(p)) for p in paths}}
                self._journal()
            return self
        except BaseException:
            if self.context:
                self.context.__exit__(*__import__("sys").exc_info())
                self.context = None
            raise

    def __exit__(self, typ, value, trace):
        try:
            if not self.readonly and self.marker.exists():
                marker = json.loads(self.marker.read_text(encoding="utf-8"))
                if marker.get("token") == self.token:
                    if typ is not None:
                        try:
                            _restore_roles_transaction(self.root, self.journal)
                        except BaseException:
                            self.journal["state"] = "recovery_required"
                            self._journal()
                            raise
                    self.journal["state"] = "rolled_back" if typ is not None else "committed"
                    self._journal()
                    self.marker.unlink()
        finally:
            if self.context:
                self.context.__exit__(typ, value, trace)

    def _journal(self):
        data = json.dumps(self.journal, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_bytes(self.root / "work/voice_history/transactions" / (self.token + ".json"), data)
        _atomic_bytes(self.marker, data)

    def _change(self, path, data, *, backup=None):
        if self.readonly:
            raise ProtectionError("режим проверки не изменяет файлы")
        path = Path(path).resolve()
        rel = str(path.relative_to(self.root))
        _safe_asset(self.root, rel)
        previous = _current(path)
        record = self.journal["changes"].get(rel)
        if record is None:
            if previous is not None:
                backup = Path(backup) if backup else self.root / "work/voice_history/transactions" / self.token / _digest(previous)
                if not backup.exists():
                    _atomic_bytes(backup, previous)
                if backup.read_bytes() != previous:
                    raise ProtectionError(f"не подтверждена копия {path.name}")
            record = {"before": _digest(previous), "backup": str(backup.relative_to(self.root)) if previous is not None else None, "after": []}
            self.journal["changes"][rel] = record
        elif _digest(previous) != record["after"][-1]:
            raise ProtectionError(f"файл изменён параллельно: {path}")
        record["after"].append(_digest(data))
        self._journal()  # write intent before a destructive operation
        if data is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_bytes(path, data)
        if _digest(_current(path)) != _digest(data):
            raise ProtectionError(f"не подтверждена запись {path}")

    def protect_candidate(self, roles):
        changed = []
        for role in self.protected:
            if role in self.original["roles"]:
                if roles["roles"].get(role) != self.original["roles"][role]:
                    changed.append(role)
                roles["roles"][role] = copy.deepcopy(self.original["roles"][role])
            old_alias = self.original.get("aliases", {}).get(role)
            if old_alias is None:
                roles.setdefault("aliases", {}).pop(role, None)
            else:
                roles.setdefault("aliases", {})[role] = old_alias
        # Keep the collision-sensitive voice_stems ordering of existing roles.
        roles["roles"] = {k: roles["roles"][k] for k in list(self.original["roles"]) +
                          [x for x in roles["roles"] if x not in self.original["roles"]] if k in roles["roles"]}
        before, after = self.n.voice_stems(self.original), self.n.voice_stems(roles)
        if any(before[k] != after.get(k) for k in self.protected if k in before):
            raise ProtectionError("изменение ролей переименовывает образец закреплённого голоса")
        return sorted(changed)

    def write_roles(self, roles):
        if self.readonly:
            raise ProtectionError("режим проверки не записывает роли")
        blocked = self.protect_candidate(roles)
        if self.n.ROLES_PATH.read_bytes() != self.initial_bytes:
            raise ProtectionError("roles.json изменён параллельно; автоматическая правка отменена")
        history = self.root / "work/voice_history" / ("roles_" + hashlib.sha256(self.initial_bytes).hexdigest() + ".json")
        _atomic_bytes(history, self.initial_bytes)
        if history.read_bytes() != self.initial_bytes:
            raise ProtectionError("не прошла проверка копии roles.json")
        self._change(self.n.ROLES_PATH, json.dumps(roles, ensure_ascii=False, indent=2).encode("utf-8"), backup=history)
        if blocked:
            print("защита редакции: сохранены прежние настройки закреплённых ролей: " + ", ".join(blocked), flush=True)

    def preserve_sample(self, role, paths=None):
        if role in self.protected:
            raise ProtectionError(f"образец роли {role} используется закреплённой редакцией")
        paths = tuple(paths or self.samples.get(role, ()))
        data = {Path(p): Path(p).read_bytes() for p in paths if Path(p).is_file()}
        if not data:
            return None
        key = hashlib.sha256(b"".join(p.name.encode() + b"\0" + hashlib.sha256(d).digest()
                                      for p, d in data.items())).hexdigest()
        folder = self.root / "work/voice_history/samples" / key
        records = {}
        for path, content in data.items():
            saved = folder / path.name
            if saved.exists() and saved.read_bytes() != content:
                raise ProtectionError(f"повреждена прежняя версия голоса {saved}")
            if not saved.exists():
                _atomic_bytes(saved, content)
            if saved.read_bytes() != content or path.read_bytes() != content:
                raise ProtectionError(f"не прошла проверка сохранения голоса {path}")
            records[path.name] = {"source": str(path), "sha256": hashlib.sha256(content).hexdigest()}
        _atomic_bytes(folder / "manifest.json", json.dumps({"v": 1, "role": role, "files": records,
                       "config": self.original["roles"].get(role)}, ensure_ascii=False, indent=2).encode("utf-8"))
        return folder

    def retire_samples(self, roles):
        count = 0
        for role in set(roles) - self.protected:
            paths = self.samples.get(role, ())
            self.preserve_sample(role, paths)
            for path in paths:
                if path.exists():
                    self._change(path, None)
                    count += 1
        return count

    def replace_samples(self, role, target_pair, source_pair):
        if role in self.protected:
            return False
        old = {Path(p): Path(p).read_bytes() if Path(p).exists() else None for p in target_pair}
        new = {Path(t): Path(s).read_bytes() if Path(s).is_file() else None
               for t, s in zip(target_pair, source_pair)}
        if next(iter(new.values())) is None:
            raise ProtectionError("нет нового WAV образца голоса")
        self.preserve_sample(role, target_pair)
        for path, data in new.items():
            self._change(path, data)
        return True

    def invalidate_wavs(self, changed, current_roles):
        changed = set(changed) - self.protected
        count, seen = 0, set()
        for i, row, old_role, wav in self.bindings:
            if (i in self.state.protected_indices or wav in self.protected_wavs or wav in seen
                    or not ({old_role, row["speaker"], self.n.resolve_role(row["speaker"], current_roles)} & changed)):
                continue
            seen.add(wav)
            if not wav.exists():
                continue
            if wav.with_suffix(".lock").exists():
                raise ProtectionError(f"запись занята воркером: {wav}; остановите озвучку перед сменой голоса")
            saved = preserve_pipeline_bundle(self.root, wav)
            for p in wav.parent.glob(wav.stem + ".*"):
                if p.is_file() and p.suffix not in (".lock", ".tmp"):
                    backup = saved / p.name if p == wav else saved / "sidecar_versions" / _file_hash(p) / p.name
                    self._change(p, None, backup=backup)
            count += 1
        return count


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Проверяемое восстановление незавершённой смены голосов")
    parser.add_argument("--root", required=True)
    parser.add_argument("--recover", action="store_true", required=True)
    args = parser.parse_args()
    print(recover_roles(args.root))
