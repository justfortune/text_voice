"""NOX_KIT 2026-11-18d — retain superseded pipeline WAV bundles on the same disk."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat

from nox_release_io import atomic_write, digest
from nox_file_transactions import directory_lock


def _safe_locations(root: Path):
    for relative in ('work/wav', 'work/audio_history', 'studio/transactions'):
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise RuntimeError('ссылка или junction в пути архива WAV: ' + str(current))
            try:
                current.resolve().relative_to(root)
            except ValueError as exc:
                raise RuntimeError('путь архива WAV выходит из книги') from exc
    wavs = root / 'work/wav'
    history = root / 'work/audio_history'
    ancestor = history
    while not ancestor.exists():
        ancestor = ancestor.parent
    if wavs.exists() and wavs.stat().st_dev != ancestor.stat().st_dev:
        raise RuntimeError('архив WAV должен находиться на том же томе, что work/wav')


def archive_stem(root: Path, stem: str, reason: str, *, target_row=None, precondition=None) -> Path | None:
    root = Path(root).resolve()
    _safe_locations(root)
    with directory_lock(root / 'studio/transactions'):
        _recover_archives(root)
        if (root / 'work/.nox_prepare.lock').exists():
            raise RuntimeError('подготовка книги не завершена; аудио не перемещено')
        if precondition is not None and precondition() is False:
            return None
        return _archive_stem(root, stem, reason, target_row=target_row)


def _archive_stem(root: Path, stem: str, reason: str, *, target_row=None) -> Path | None:
    root = Path(root).resolve()
    wavs = root / "work/wav"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
        raise ValueError("неверный ключ записи")
    if (wavs / (stem + ".lock")).exists():
        raise RuntimeError("запись занята воркером: " + stem)
    paths = sorted(p for p in wavs.glob(stem + ".*") if p.is_file() and not p.is_symlink())
    if not paths:
        return None
    op = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(8)
    directory = root / "work/audio_history" / op
    directory.mkdir(parents=True, exist_ok=False)
    # The current row may be different from what the old audio said: never relabel it as source_raw.
    text_file = wavs / (stem + ".txt")
    source_tech = text_file.read_text(encoding="utf-8-sig") if text_file.exists() else None
    manifest = {"format": 1, "kind": "pipeline_audio", "state": "prepared", "revision": op,
                "reason": str(reason), "stem": stem, "source_tech": source_tech,
                "target_row": target_row, "files": [
                    {"name": p.name, "original": str(p.relative_to(root)).replace("\\", "/"),
                     "sha256": digest(p), "bytes": p.stat().st_size} for p in paths]}
    jp = directory / "manifest.json"
    atomic_write(jp, json.dumps(manifest, ensure_ascii=False, indent=2))
    moved = []
    try:
        for p, item in zip(paths, manifest["files"]):
            if digest(p) != item["sha256"]:
                raise RuntimeError("запись изменена во время архивирования")
            destination = directory / p.name
            os.replace(p, destination)
            moved.append((p, destination, item["sha256"]))
            if digest(destination) != item["sha256"]:
                raise RuntimeError("контрольная сумма архива записи не совпала")
        manifest["state"] = "archived"
        atomic_write(jp, json.dumps(manifest, ensure_ascii=False, indent=2))
        return directory
    except BaseException:
        errors = []
        for original, archived, expected in reversed(moved):
            try:
                if original.exists() or digest(archived) != expected:
                    raise RuntimeError("конфликт возврата записи")
                os.replace(archived, original)
            except Exception as exc:
                errors.append(str(exc))
        manifest["state"] = "recovery_required" if errors else "rolled_back"
        manifest["errors"] = errors
        atomic_write(jp, json.dumps(manifest, ensure_ascii=False, indent=2))
        if errors:
            raise RuntimeError("прервано архивирование; нужен журнал " + str(jp))
        raise


def pending_archives(root: Path) -> list[Path]:
    pending = []
    for p in (Path(root) / "work/audio_history").glob("*/manifest.json"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("state") not in ("archived", "rolled_back"):
            pending.append(p)
    return pending


def _recover_archives(root: Path) -> int:
    """Restore every interrupted move as a bundle, after validating all its files."""
    count = 0
    for jp in pending_archives(root):
        doc = json.loads(jp.read_text(encoding='utf-8'))
        if doc.get('format') != 1 or doc.get('kind') != 'pipeline_audio' or not isinstance(doc.get('files'), list):
            raise RuntimeError('повреждён журнал сохранения WAV: ' + str(jp))
        plan, seen = [], set()
        for item in doc['files']:
            name = item['name']
            if Path(name).name != name or '/' in name or '\\' in name or name in seen:
                raise RuntimeError('неверное имя в журнале WAV')
            seen.add(name)
            original = root / 'work/wav' / name
            if item.get('original') != original.relative_to(root).as_posix():
                raise RuntimeError('неверное исходное место в журнале WAV')
            saved = jp.parent / name
            for p in (original, saved):
                try:
                    p.resolve().relative_to(root)
                except ValueError as exc:
                    raise RuntimeError('путь WAV выходит из книги') from exc
                if p.is_symlink():
                    raise RuntimeError('ссылка вместо WAV в журнале')
            a = digest(original) if original.is_file() else None
            b = digest(saved) if saved.is_file() else None
            expected = item['sha256']
            if (a, b) not in ((expected, None), (None, expected)):
                raise RuntimeError('конфликт восстановления WAV; исходники сохранены: ' + str(jp))
            if b is not None:
                plan.append((saved, original))
        for saved, original in reversed(plan):
            if original.exists():
                raise RuntimeError('WAV появился во время восстановления')
            os.replace(saved, original)
        doc['state'] = 'rolled_back'
        doc['recovered'] = True
        atomic_write(jp, json.dumps(doc, ensure_ascii=False, indent=2))
        count += 1
    return count


def recover_archives(root: Path) -> int:
    root = Path(root).resolve()
    _safe_locations(root)
    with directory_lock(root / 'studio/transactions'):
        return _recover_archives(root)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Проверяемое восстановление прерванного перемещения прежних WAV')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    if args.recover:
        print('Восстановлено прерванных операций:', recover_archives(args.root))
    else:
        for path in pending_archives(args.root):
            print(path)
