"""NOX_KIT 2026-11-18d — checked code-only update of an existing NOX book.

No package installation, cleanup, segmentation, model loading or audio rewrite.
The package manifest explicitly names every destination. Unknown local code is
reported instead of overwritten. Backups retain the exact original bytes.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import shutil
import subprocess
import sys
import zipfile

from nox_release_io import atomic_write, digest


class UpdateError(RuntimeError):
    pass


def normalized_hash(data: bytes) -> str:
    return hashlib.sha256(data.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")).hexdigest()


def relative(value: str) -> Path:
    p = PurePosixPath(value)
    if not value or "\\" in value or p.is_absolute() or ".." in p.parts or ":" in value:
        raise UpdateError("недопустимый относительный путь: " + str(value))
    return Path(*p.parts)


def child(root: Path, name: str) -> Path:
    p = Path(root) / relative(name)
    try:
        p.resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise UpdateError("путь назначения выходит из выбранного каталога: " + str(p)) from exc
    for q in (p,) + tuple(p.parents):
        if q == Path(root).parent:
            break
        if q.is_symlink():
            raise UpdateError("ссылка в пути заменяемого кода: " + str(q))
        try:
            if q.exists() and q.stat().st_file_attributes & 0x400:
                raise UpdateError("точка повторной обработки в пути кода: " + str(q))
        except AttributeError:
            pass
    return p


def read_manifest(bundle: Path) -> dict:
    try:
        manifest = json.loads((bundle / "release_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError("нет читаемого release_manifest.json") from exc
    if manifest.get("format") != 1 or manifest.get("product_version") != "2026-11-18d":
        raise UpdateError("неподдерживаемый формат комплекта")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(manifest.get("build_id", ""))):
        raise UpdateError("неверный идентификатор сборки")
    seen = set()
    for item in manifest.get("files", []):
        name = item["source"]
        p = child(bundle, name)
        if name in seen or not p.is_file() or digest(p) != item["sha256"]:
            raise UpdateError("контрольная сумма/структура комплекта не совпала: " + name)
        seen.add(name)
    if not seen or any(t.get("source") not in seen for t in manifest.get("targets", [])):
        raise UpdateError("пустой или неполный список файлов комплекта")
    return manifest


def process_uses_path(command: str, path: Path) -> bool:
    # Boundary-aware: D:\kniga1a must never match D:\kniga1a_old.
    cmd = command.replace("/", "\\").casefold()
    needle = str(path).replace("/", "\\").rstrip("\\").casefold()
    start = 0
    while (at := cmd.find(needle, start)) >= 0:
        before = cmd[at - 1] if at else " "
        after = cmd[at + len(needle):at + len(needle) + 1]
        if before in " \t\r\n\"'=" and (not after or after in "\\ \t\r\n\"'"):
            return True
        start = at + 1
    return False


def running_writers(book: Path, home: Path) -> list:
    if os.name != "nt":
        return []
    code = "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false; $ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(pythonw?|powershell|pwsh|ffmpeg)\\.exe$' } | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress"
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                                capture_output=True, timeout=30, check=True)
        raw = result.stdout.decode("utf-8-sig", "replace").strip()
        objects = json.loads(raw) if raw else []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise UpdateError("не удалось проверить работающие процессы; обновление не начинается") from exc
    if isinstance(objects, dict):
        objects = [objects]
    relevant = []
    for item in objects:
        if int(item.get("ProcessId", -1)) in (os.getpid(), os.getppid()):
            continue
        cmd = item.get("CommandLine") or ""
        if not cmd:
            # Unreadable unrelated service commands are not guessed to be NOX.
            continue
        if "nox_update.py" in cmd or "Update_NOX_18d.ps1" in cmd:
            continue
        if process_uses_path(cmd, book) or process_uses_path(cmd, home / "studio"):
            relevant.append({"pid": item["ProcessId"], "name": item["Name"]})
    return relevant


def metadata_files(book: Path) -> list[Path]:
    """Bounded to book metadata; never visit models, envs, audio or browser data."""
    result = []
    for dirname in ("work", "studio"):
        directory = book / dirname
        if directory.is_dir():
            for p in directory.iterdir():
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in (".json", ".csv", ".tsv", ".sig", ".ver"):
                    if p.name != "nox_release.json":
                        result.append(p)
    return sorted(result)


def state_hashes(book: Path) -> dict:
    return {str(p.relative_to(book)).replace("\\", "/"): digest(p) for p in metadata_files(book)}


def check_project(book: Path) -> dict:
    segments = book / "work/segments.csv"
    if not segments.is_file():
        raise UpdateError("нет work/segments.csv; это обновление существующей подготовленной книги")
    before = state_hashes(book)
    try:
        with segments.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
        if not rows or "text" not in rows[0]:
            raise UpdateError("неподдерживаемая или пустая разбивка segments.csv")
        # This helper is bundled with this release, not imported from the old book.
        from nox_freeze import read_protection
        from nox_audio_archive import pending_archives
        for marker in ('work/.nox_prepare.lock', 'work/.nox_roles.lock', 'maintenance/active.json'):
            if (book / marker).exists():
                raise UpdateError('есть незавершённая операция книги: ' + marker)
        pending = pending_archives(book)
        if pending:
            raise UpdateError('есть незавершённое сохранение прежней озвучки: ' + str(pending[0]))
        protection = read_protection(book, rows, check_audio=True)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("состояние закреплений требует разбора до обновления: " + str(exc)) from exc
    after = state_hashes(book)
    if before != after:
        raise UpdateError("состояние книги изменилось во время проверки; дождитесь завершения операций и закройте Studio")
    return {"rows": len(rows), "fixed_parts": len(protection.ranges),
            "protected_rows": len(protection.protected_indices),
            "part_selected_phrases": len(getattr(protection, "part_selected_uids", ())),
            "state_hashes": after}


def make_plan(bundle: Path, manifest: dict, book: Path, home: Path, downloads: Path | None = None) -> list:
    roots = {"book": book, "app": home / "studio", "home": home, "downloads": downloads}
    source_hashes = {x['source']: x['sha256'] for x in manifest['files']}
    plan, seen = [], set()
    for item in manifest["targets"]:
        scope = item["scope"]
        if scope not in roots or roots[scope] is None:
            if scope == "downloads":
                continue
            raise UpdateError("неизвестная область назначения")
        target = child(roots[scope], item["path"])
        if item.get("existing_only") and not target.exists():
            continue
        if str(target).casefold() in seen:
            raise UpdateError("повторное назначение: " + str(target))
        seen.add(str(target).casefold())
        source = child(bundle, item["source"])
        new_hash = digest(source)
        if source_hashes.get(item['source']) != new_hash:
            raise UpdateError('файл комплекта изменился после проверки манифеста: ' + item['source'])
        old_hash = digest(target) if target.is_file() else None
        if target.exists() and not target.is_file():
            raise UpdateError("на месте файла находится каталог: " + str(target))
        supported = item.get("known_previous", [])
        compatible = old_hash is None or old_hash == new_hash or normalized_hash(target.read_bytes()) in supported
        plan.append({"source": str(source), "target": str(target), "old_hash": old_hash,
                     "new_hash": new_hash, "compatible": compatible, "scope": scope,
                     "relative": item["path"], "changed": old_hash != new_hash})
    return plan


def verify_installed(bundle: Path, manifest: dict, book: Path, home: Path, scope: str):
    """Read-only check used by the launcher; never copy a partial update."""
    plan = make_plan(bundle, manifest, book, home)
    selected = {'app'} if scope == 'app' else {'book', 'app', 'home'}
    wrong = [x['target'] for x in plan if x['scope'] in selected and x['changed']]
    if wrong:
        raise UpdateError('установленный код отличается от полного комплекта 18d: ' + '; '.join(wrong) +
                          '. Закройте операции NOX и примените Update_NOX_18d.ps1; запуск ничего не заменяет.')


def unfinished_updates(home: Path) -> list[Path]:
    pending = []
    for journal in sorted((home / 'update_backups').glob('*/update.json')):
        try:
            doc = json.loads(journal.read_text(encoding='utf-8'))
            if doc.get('kind') != 'nox_code_update' or doc.get('state') not in ('committed', 'rolled_back'):
                pending.append(journal)
        except (OSError, ValueError):
            pending.append(journal)
    for journal in sorted((home / 'new_app_transactions').glob('*.json')):
        try:
            doc = json.loads(journal.read_text(encoding='utf-8'))
            if doc.get('state') not in ('committed', 'rolled_back'):
                pending.append(journal)
        except (OSError, ValueError):
            pending.append(journal)
    return pending


def install_new_app(bundle: Path, manifest: dict, book: Path, home: Path):
    from nox_file_transactions import directory_lock
    from nox_release_io import recover_publications
    with directory_lock(home / 'update_guard'):
        with directory_lock(home / 'new_app_transactions'):
            recover_publications(home / 'new_app_transactions', home)
            if unfinished_updates(home):
                raise UpdateError('не завершено прежнее обновление кода; сначала восстановите его по журналу')
            return _install_new_app_locked(bundle, manifest, book, home)


def _install_new_app_locked(bundle: Path, manifest: dict, book: Path, home: Path):
    """Bootstrap the common editor only in a fresh installation.

    Existing prepared books always go through the full checked updater. This
    path lets the separate clean-machine installer retain its import-new-book
    workflow without silently updating an existing working editor.
    """
    if (book / 'work/segments.csv').exists():
        raise UpdateError('книга уже подготовлена; используйте полное обновление, а не новую установку')
    if (home / 'studio/nox_release.json').exists():
        verify_installed(bundle, manifest, book, home, 'app')
        return
    plan = [x for x in make_plan(bundle, manifest, book, home) if x['scope'] == 'app']
    if any(x['old_hash'] not in (None, x['new_hash']) for x in plan):
        raise UpdateError('редактор уже установлен; его заменяет только полное проверяемое обновление')
    from nox_release_io import Publication
    from nox_file_transactions import directory_lock
    stage = home / 'new_app_staging' / secrets.token_hex(10)
    stage.mkdir(parents=True)
    pairs = []
    for i, item in enumerate(plan):
        if item['changed']:
            temp = stage / str(i)
            atomic_copy_code(Path(item['source']), temp, item['new_hash'])
            pairs.append((temp, Path(item['target'])))
    record = {'version': manifest['product_version'], 'build': manifest['build_id'], 'home': str(home),
              'kit_dir': str(bundle / 'kit'), 'bundle_dir': str(bundle), 'base_commit': manifest['base_commit']}
    temp = stage / 'record.json'
    atomic_write(temp, json.dumps(record, ensure_ascii=False, indent=2))
    pairs.append((temp, home / 'studio/nox_release.json'))
    tx = Publication(home / 'new_app_transactions', pairs, allowed_root=home)
    tx.publish()
    try:
        tx.commit()
    except BaseException:
        if tx.doc['state'] == 'published':
            tx.rollback()
        raise
    shutil.rmtree(stage)


def save_diagnostics(output: Path, book: Path, home: Path, plan: list, issues: list, project=None) -> Path:
    """Small source+state archive for an unknown build, explicitly excluding WAV."""
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    path = output / ("NOX_18d_preflight_" + stamp + ".zip")
    report = {"at": datetime.now().astimezone().isoformat(), "book": str(book), "home": str(home),
              "python": sys.executable, "issues": issues, "project": project, "files": plan}
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as z:
        z.writestr("preflight.json", json.dumps(report, ensure_ascii=False, indent=2))
        seen = set()
        for item in plan:
            p = Path(item["target"])
            name = item["scope"] + "/" + item["relative"]
            if p.is_file() and p.stat().st_size < 16 * 1024 * 1024 and name not in seen:
                z.write(p, name)
                seen.add(name)
        for p in metadata_files(book):
            name = "book/" + p.relative_to(book).as_posix()
            if p.stat().st_size <= 64 * 1024 * 1024 and name not in seen:
                z.write(p, name)
                seen.add(name)
    return path


def atomic_copy_code(source: Path, destination: Path, expected: str):
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise UpdateError("файл комплекта изменился во время установки: " + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(destination, data)
    if digest(destination) != expected:
        raise UpdateError("контрольная сумма установленного кода не совпала")


def restore_code(journal: Path, *, expected_book: Path, expected_home: Path,
                 expected_downloads: Path | None = None, check_state=True):
    doc = json.loads(journal.read_text(encoding="utf-8"))
    if doc.get("format") != 1 or doc.get("kind") != "nox_code_update":
        raise UpdateError("неверный журнал отката кода")
    book = Path(doc["book"])
    home = Path(doc["home"])
    if book.resolve() != Path(expected_book).resolve() or home.resolve() != Path(expected_home).resolve():
        raise UpdateError("журнал отката относится к другой книге или установке")
    try:
        journal.resolve().relative_to(home.resolve() / "update_backups")
    except ValueError as exc:
        raise UpdateError("журнал находится вне каталога резервных копий этой установки") from exc
    downloads = doc.get("downloads")
    if downloads and (expected_downloads is None or Path(downloads).resolve() != Path(expected_downloads).resolve()):
        raise UpdateError("каталог запуска в журнале не соответствует выбранному")
    roots = {"book": book, "home": home, "app": home / "studio", "downloads": expected_downloads}
    targets = set()
    for item in doc["items"]:
        scope = item.get("scope")
        if scope not in roots or roots[scope] is None:
            raise UpdateError("неизвестная область файла в журнале")
        target = child(roots[scope], item["relative"])
        if target.resolve() != Path(item["target"]).resolve() or str(target).casefold() in targets:
            raise UpdateError("неверный путь назначения в журнале")
        targets.add(str(target).casefold())
        if not re.fullmatch(r"files/[0-9]{4}\.bin", item["backup"]):
            raise UpdateError("неверный путь резервной копии в журнале")
        child(journal.parent, item["backup"])
    if check_state and state_hashes(book) != doc["book_state"]:
        raise UpdateError("после обновления состояние книги менялось; автоматический откат кода остановлен")
    errors = []
    # Validate all backups before the first restoration; never partly roll back a corrupt backup.
    for item in doc["items"]:
        target = Path(item["target"])
        actual = digest(target) if target.exists() else None
        if actual not in (item["new_hash"], item["old_hash"]):
            raise UpdateError("код после обновления изменён: " + str(target))
        if item["old_hash"] is not None:
            backup = journal.parent / relative(item["backup"])
            if not backup.is_file() or digest(backup) != item["old_hash"]:
                raise UpdateError("неверная резервная копия: " + str(backup))
    for item in reversed(doc["items"]):
        try:
            target = Path(item["target"])
            if item["old_hash"] is None:
                if target.exists():
                    target.unlink()
            else:
                atomic_copy_code(journal.parent / item["backup"], target, item["old_hash"])
        except OSError as exc:
            errors.append(str(target) + ": " + str(exc))
    doc["state"] = "rollback_failed" if errors else "rolled_back"
    doc["errors"] = errors
    atomic_write(journal, json.dumps(doc, ensure_ascii=False, indent=2))
    if errors:
        raise UpdateError("откат требует восстановления по журналу: " + str(journal))


def install(bundle: Path, manifest: dict, book: Path, home: Path, plan: list, project: dict) -> Path:
    if any(not item['compatible'] for item in plan):
        raise UpdateError('есть неизвестные локальные файлы; замена остановлена')
    if state_hashes(book) != project['state_hashes']:
        raise UpdateError('книга изменилась после предварительной проверки')
    from nox_file_transactions import directory_lock
    with directory_lock(home / 'update_guard'):
        return _install_locked(bundle, manifest, book, home, plan, project)


def _install_locked(bundle: Path, manifest: dict, book: Path, home: Path, plan: list, project: dict) -> Path:
    pending = unfinished_updates(home)
    if pending:
        raise UpdateError('незавершённое прежнее обновление; сначала восстановите код по журналу: ' + str(pending[0]))
    if any(not item["compatible"] for item in plan):
        raise UpdateError("есть неизвестные локальные файлы; замена остановлена")
    if state_hashes(book) != project["state_hashes"]:
        raise UpdateError("книга изменилась после предварительной проверки")
    build = manifest["build_id"]
    kit_root = child(home, "kits/" + build)
    if kit_root.exists():
        # Reuse only an identical immutable payload, never overwrite a modified installation.
        stored_manifest = kit_root / 'release_manifest.json'
        if not stored_manifest.is_file() or stored_manifest.read_bytes() != (bundle / 'release_manifest.json').read_bytes():
            raise UpdateError('манифест установленного неизменяемого комплекта отсутствует или изменён: ' + str(stored_manifest))
        for item in manifest["files"]:
            p = child(kit_root, item["source"])
            if not p.is_file() or digest(p) != item["sha256"]:
                raise UpdateError("папка установленного комплекта изменена: " + str(p))
    else:
        stage = home / "kits" / (".stage-" + secrets.token_hex(10))
        stage.mkdir(parents=True, exist_ok=False)
        try:
            for item in manifest["files"]:
                atomic_copy_code(child(bundle, item["source"]), child(stage, item["source"]), item["sha256"])
            atomic_write(stage / "release_manifest.json", (bundle / "release_manifest.json").read_bytes())
            os.replace(stage, kit_root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # Release records belong to this update and participate in the code rollback.
    record = {"version": manifest["product_version"], "build": build, "kit_dir": str(kit_root / "kit"), "home": str(home),
              "bundle_dir": str(kit_root), "installed_at": datetime.now().astimezone().isoformat(),
              "base_commit": manifest["base_commit"]}
    record_bytes = json.dumps(record, ensure_ascii=False, indent=2).encode()
    record_source = kit_root / (".record-" + secrets.token_hex(8) + ".json")
    atomic_write(record_source, record_bytes)
    effective = [dict(item) for item in plan if item["changed"]]
    for target, scope, rel in ((book / "work/nox_release.json", "book", "work/nox_release.json"),
                               (home / "studio/nox_release.json", "app", "nox_release.json")):
        effective.append({"source": str(record_source), "target": str(target),
                          "old_hash": digest(target) if target.exists() else None,
                          "new_hash": hashlib.sha256(record_bytes).hexdigest(), "compatible": True,
                          "scope": scope, "relative": rel})
    need = sum(Path(i["source"]).stat().st_size + (Path(i["target"]).stat().st_size if Path(i["target"]).exists() else 0)
               for i in effective)
    if shutil.disk_usage(home).free < need + 32 * 1024 * 1024:
        raise UpdateError("недостаточно места для кода и его резервной копии; озвучка не затрагивалась")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    backup_dir = home / "update_backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    download_roots = {str(Path(x["target"]).parent.resolve()) for x in effective if x["scope"] == "downloads"}
    if len(download_roots) > 1:
        raise UpdateError("несогласованные каталоги запуска в плане")
    downloads = Path(next(iter(download_roots))) if download_roots else None
    doc = {"format": 1, "kind": "nox_code_update", "state": "prepared", "build": build,
           "book": str(book), "home": str(home), "downloads": str(downloads) if downloads else None,
           "book_state": project["state_hashes"], "items": []}
    for index, item in enumerate(effective):
        target = Path(item["target"])
        if (digest(target) if target.exists() else None) != item["old_hash"]:
            raise UpdateError("файл изменился перед резервированием: " + str(target))
        entry = dict(item, backup=f"files/{index:04d}.bin")
        if item["old_hash"] is not None:
            atomic_copy_code(target, backup_dir / entry["backup"], item["old_hash"])
        doc["items"].append(entry)
    journal = backup_dir / "update.json"
    atomic_write(journal, json.dumps(doc, ensure_ascii=False, indent=2))
    try:
        if state_hashes(book) != project["state_hashes"]:
            raise UpdateError("книга изменилась во время резервирования")
        doc["state"] = "installing"
        atomic_write(journal, json.dumps(doc, ensure_ascii=False, indent=2))
        for item in doc["items"]:
            target = Path(item["target"])
            if (digest(target) if target.exists() else None) != item["old_hash"]:
                raise UpdateError("код изменился перед заменой: " + str(target))
            atomic_copy_code(Path(item["source"]), target, item["new_hash"])
        if state_hashes(book) != project["state_hashes"]:
            raise UpdateError("состояние книги изменилось во время обновления")
        doc["state"] = "committed"
        atomic_write(journal, json.dumps(doc, ensure_ascii=False, indent=2))
    except BaseException:
        restore_code(journal, expected_book=book, expected_home=home,
                     expected_downloads=downloads, check_state=False)
        raise
    finally:
        record_source.unlink(missing_ok=True)
    return journal


def main(argv=None):
    parser = argparse.ArgumentParser(description="Обновление существующей NOX до 18d с проверкой совместимости")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path("D:/nox_audiobook"))
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--downloads", type=Path)
    parser.add_argument("--output", type=Path, default=Path.cwd())
    parser.add_argument('--no-diagnostics', action='store_true')
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-bundle", action="store_true")
    parser.add_argument("--verify-installed", choices=('app', 'all'))
    parser.add_argument('--install-new-app', action='store_true')
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args(argv)
    book, home, bundle = args.root.resolve(), args.home.resolve(), args.bundle.resolve()
    plan, project = [], None
    try:
        if args.rollback:
            if os.name != "nt":
                raise UpdateError("откат установленного приложения выполняется на Windows")
            if running_writers(book, home):
                raise UpdateError("сначала штатно остановите операции NOX и закройте Studio")
            from nox_file_transactions import directory_lock
            with directory_lock(home / 'update_guard'):
                restore_code(args.rollback, expected_book=book, expected_home=home, expected_downloads=args.downloads)
            print("Прежний код восстановлен. Состояние книги не заменялось.")
            return 0
        manifest = read_manifest(bundle)
        if args.verify_bundle:
            print("Комплект " + manifest["product_version"] + ": контрольные суммы совпали.")
            return 0
        if args.verify_installed:
            verify_installed(bundle, manifest, book, home, args.verify_installed)
            print('Установленный комплект 18d проверен; файлы не заменялись.')
            return 0
        if args.install_new_app:
            if os.name != 'nt':
                raise UpdateError('новая установка приложения выполняется на Windows')
            if running_writers(book, home):
                raise UpdateError('сначала закройте операции NOX')
            install_new_app(bundle, manifest, book, home)
            print('Редактор для новой установки подготовлен; книги и аудио не менялись.')
            return 0
        plan = make_plan(bundle, manifest, book, home, args.downloads)
        issues = []
        pending = unfinished_updates(home)
        if pending:
            issues.append('незавершённое прежнее обновление: ' + str(pending[0]))
        unknown = [x["target"] for x in plan if not x["compatible"]]
        if unknown:
            issues.append("неизвестные локальные версии: " + "; ".join(unknown))
        active = running_writers(book, home)
        if active:
            issues.append("NOX ещё работает: " + ", ".join(str(x["pid"]) + " " + x["name"] for x in active))
            print("СТОП: " + " | ".join(issues))
            print("Сначала штатно завершите операции NOX и закройте окно Studio. "
                  "Проверка закреплений начнётся после завершения её фоновых процессов.")
            print("Файлы рабочей установки не заменялись; снимок изменяемого проекта не создавался.")
            return 2
        try:
            project = check_project(book)
        except UpdateError as exc:
            issues.append(str(exc))
        if issues:
            print("СТОП: " + " | ".join(issues))
            if not args.no_diagnostics:
                report = save_diagnostics(args.output, book, home, plan, issues, project)
                print("Диагностика без аудио: " + str(report))
            print("Файлы рабочей установки не заменялись.")
            return 2
        print(f"Совместимость кода проверена. Фраз: {project['rows']}; закреплённых частей: {project['fixed_parts']}.")
        if project.get('part_selected_phrases'):
            print("В закреплённых частях есть выбранные дубли без индивидуального закрепления: "
                  + str(project['part_selected_phrases'])
                  + ". Дубли проверены; правки и снятое закрепление сохраняются.")
        print("Заменяемых файлов: " + str(sum(x["changed"] for x in plan)))
        if not args.apply:
            print("Проверка завершена; обновление не применялось.")
            return 0
        if os.name != "nt":
            raise UpdateError("установка приложения выполняется на Windows")
        if running_writers(book, home):
            raise UpdateError("во время проверки запущен процесс NOX; повторите после его штатного завершения")
        journal = install(bundle, manifest, book, home, plan, project)
        print("NOX 2026-11-18d установлена. Разбивка, правки и аудио сохранены.")
        print("Журнал и резервная копия заменённого кода: " + str(journal))
        print("Запуск: ярлык Nox Studio или nox_studio.ps1 из установленного комплекта.")
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print("СТОП: " + str(exc), file=sys.stderr)
        if args.verify_bundle or args.verify_installed or args.no_diagnostics:
            return 2
        try:
            path = save_diagnostics(args.output, book, home, plan, [str(exc)], project)
            print("Диагностика: " + str(path), file=sys.stderr)
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
