"""NOX 18d bundle inspection and main-only code publication.

No book text, audio, local environment inventory or conversation export is
collected by the publisher. All publishable bytes come from a checked bundle.
Git history is extended normally; an advanced remote requires human review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kit"))
from nox_update import (UpdateError, read_manifest, child, normalized_hash, make_plan,
                        check_project, running_writers, unfinished_updates, save_diagnostics)
from nox_release_io import atomic_write, digest
from nox_status import status


class PublishError(RuntimeError):
    pass


PREFIXES = {"kit", "tools", "base", "data", "docs", "tests", "runtime"}
EXTENSIONS = {".py", ".ps1", ".html", ".js", ".cjs", ".css", ".md", ".txt", ".json",
              ".yml", ".yaml", ".toml", ".ini", ".cfg", ".lock", ".csv"}
PRIVATE_NAME = re.compile(r"^(?:segments|roles|edits|fixed_parts|sid_map|auto_book|nox_release|speaker_overrides|stress_fixes|book|kniga\d)[^/]*\.(?:json|csv|tsv|txt)$", re.I)
TRANSFER_ID = re.compile(rb"(?:file_[0-9a-f]{24,}|libfile_[0-9a-z]{12,}|chat_files/[0-9a-f]{20,}|/workspace/scratch/[0-9a-f]{8,}|claude-transfer_[^\s\"']+\.zip)", re.I)


def _public_file(name, data):
    path = Path(name)
    special = path.name in (".gitignore", ".gitattributes") and len(path.parts) == 1
    if not special:
        if len(path.parts) > 1 and path.parts[0] not in PREFIXES:
            raise PublishError("файл вне каталогов исходного комплекта: " + name)
        if path.suffix.lower() not in EXTENSIONS or PRIVATE_NAME.match(path.name):
            raise PublishError("файл не относится к публикуемому коду/документации: " + name)
    if path.parts[:2] == ("tests", "generated_runtime"):
        raise PublishError("временный runtime тестов не публикуется: " + name)
    if b"\0" in data or TRANSFER_ID.search(data):
        raise PublishError("в файле обнаружены двоичные данные или идентификаторы личного переноса: " + name)
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PublishError("ожидался текстовый исходник UTF-8: " + name) from exc


def public_payload(bundle):
    bundle = Path(bundle).resolve()
    manifest = read_manifest(bundle)
    payload = {}
    for item in manifest["files"]:
        name = item["source"]
        data = child(bundle, name).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise PublishError("исходник изменился после проверки manifest: " + name)
        _public_file(name, data)
        payload[name] = data
    # The release manifest contains code checksums and supported code revisions,
    # never hashes collected from the user's live book by this publisher.
    data = (bundle / "release_manifest.json").read_bytes()
    if json.loads(data.decode("utf-8")) != manifest:
        raise PublishError("manifest изменился во время подготовки публикации")
    _public_file("release_manifest.json", data)
    payload["release_manifest.json"] = data
    return manifest, payload


def git(repo, *args, check=True):
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(name, None)
    result = subprocess.run(["git", "-C", str(repo), *map(str, args)], capture_output=True,
                            env=env, timeout=120)
    if check and result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise PublishError("git " + str(args[0]) + ": " + message)
    return result


def _text(repo, *args):
    return git(repo, *args).stdout.decode("utf-8", "strict").strip()


def _names(raw):
    return [p.decode("utf-8", "strict") for p in raw.split(b"\0") if p]


def _inside(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _check_clean(repo):
    if git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
        raise PublishError("в main есть несохранённые или посторонние файлы; сначала отдельно разберите их. Они не заменены")


def _current_tree(repo, revision, allow):
    entries = _names(git(repo, "ls-tree", "-r", "--name-only", "-z", revision).stdout)
    extra = sorted(set(entries) - set(allow))
    if extra:
        raise PublishError("в истории/дереве main есть файлы вне manifest: " + ", ".join(extra[:8]))
    for name in entries:
        mode = _text(repo, "ls-tree", revision, "--", name).split(" ", 1)[0]
        if mode not in ("100644", "100755"):
            raise PublishError("ссылка или подмодуль не публикуется автоматически: " + name)
    return entries


def _check_ahead_history(repo, remote_head, head, allow):
    commits = _text(repo, "rev-list", remote_head + ".." + head).splitlines()
    if len(commits) > 100:
        raise PublishError("более 100 неопубликованных коммитов требуют отдельной проверки истории")
    for commit in commits:
        _current_tree(repo, commit, allow)
        changed = _names(git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout)
        for name in changed:
            if name not in allow:
                raise PublishError("неопубликованная история затрагивает файл вне manifest: " + name)
            blob = git(repo, "show", commit + ":" + name, check=False)
            if blob.returncode == 0:
                _public_file(name, blob.stdout)


def _remote(repo, url):
    origin = _text(repo, "remote", "get-url", "origin")
    if origin.rstrip("/").removesuffix(".git") != str(url).rstrip("/").removesuffix(".git"):
        raise PublishError("origin указывает на другой адрес; автоматически он не перенастраивается")
    # Fetch only main. Neither checkout, merge, reset nor another branch is used
    # to make an advanced remote appear compatible with the local tree.
    git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "origin", "refs/heads/main")
    return _text(repo, "rev-parse", "FETCH_HEAD")


def inspect_publication(bundle, repo, remote, book=None, home=None):
    repo = Path(repo).resolve()
    manifest, payload = public_payload(bundle)
    for protected in (book, home):
        if protected is not None and (_inside(repo, Path(protected)) or _inside(Path(protected), repo)):
            raise PublishError("каталог публикации пересекается с книгой или установленной средой")
    for marker in ("work", "studio", "gotovo", "book.docx"):
        if (repo / marker).exists():
            raise PublishError("каталог публикации похож на рабочую книгу: " + marker)
    if not repo.is_dir() or not (repo / ".git").exists():
        raise PublishError("нужна отдельная существующая копия main: git clone --branch main <Repo> <Dir>")
    if Path(_text(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise PublishError("Dir должен быть корнем отдельного Git-репозитория")
    if _text(repo, "symbolic-ref", "--short", "HEAD") != "main":
        raise PublishError("активная ветка должна быть main; другие ветки не переключаются и не меняются")
    _check_clean(repo)
    head = _text(repo, "rev-parse", "HEAD")
    remote_head = _remote(repo, remote)
    if git(repo, "merge-base", "--is-ancestor", remote_head, head, check=False).returncode:
        raise PublishError("main на сервере продвинулась или разошлась с локальной. Нужна отдельная сверка; история не заменена")
    base = str(manifest.get("base_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", base) or git(repo, "merge-base", "--is-ancestor", base, head, check=False).returncode:
        raise PublishError("не подтверждена базовая ревизия main, из которой собран комплект")
    _current_tree(repo, head, payload)
    _check_ahead_history(repo, remote_head, head, payload)
    known = {}
    for target in manifest.get("targets", []):
        known.setdefault(target["source"], set()).update(target.get("known_previous") or [])
    changed = []
    for name, new in payload.items():
        path = child(repo, name)
        current = path.read_bytes() if path.is_file() else None
        if current == new:
            continue
        if current is not None:
            previous = git(repo, "show", base + ":" + name, check=False)
            if not ((previous.returncode == 0 and current == previous.stdout)
                    or normalized_hash(current) in known.get(name, set())):
                raise PublishError("локальный main содержит неизвестную редакцию файла; она не заменена: " + name)
        changed.append(name)
    return {"repo": repo, "manifest": manifest, "payload": payload, "head": head,
            "remote_head": remote_head, "changed": sorted(changed), "remote": str(remote)}


def _verify_tree(repo, revision, payload):
    _current_tree(repo, revision, payload)
    for name, wanted in payload.items():
        if git(repo, "show", revision + ":" + name).stdout != wanted:
            raise PublishError("коммит не совпал с проверенным комплектом: " + name)


def publish_bundle(bundle, repo, remote, *, action="plan", book=None, home=None, message=None):
    if action not in ("plan", "prepare", "push"):
        raise PublishError("неизвестное действие публикации")
    plan = inspect_publication(bundle, repo, remote, book, home)
    result = {"action": action, "head": plan["head"], "changed": plan["changed"], "pushed": False}
    if action == "plan":
        return result
    repo = plan["repo"]
    stage = None
    if plan["changed"]:
        gitdir = Path(_text(repo, "rev-parse", "--absolute-git-dir"))
        stage = gitdir / "nox-publish" / ("worktree-" + uuid.uuid4().hex)
        stage.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", str(stage), plan["head"])
        try:
            for name in plan["changed"]:
                atomic_write(child(stage, name), plan["payload"][name])
            git(stage, "add", "--", *plan["changed"])
            staged = set(_names(git(stage, "diff", "--cached", "--name-only", "-z").stdout))
            if staged != set(plan["changed"]):
                raise PublishError("индекс подготовленной версии не совпал с manifest")
            git(stage, "commit", "-m", message or ("NOX 18d: " + plan["manifest"]["build_id"]))
            new_head = _text(stage, "rev-parse", "HEAD")
            _verify_tree(stage, new_head, plan["payload"])
            _check_clean(stage)
            _check_clean(repo)
            if _text(repo, "rev-parse", "HEAD") != plan["head"]:
                raise PublishError("локальная main изменилась во время подготовки")
            if _remote(repo, remote) != plan["remote_head"]:
                raise PublishError("удалённая main изменилась во время подготовки; готовый коммит оставлен для сверки")
            git(repo, "merge", "--ff-only", new_head)
            result["head"] = new_head
            git(repo, "worktree", "remove", str(stage))
            stage = None
        except Exception as exc:
            raise PublishError(str(exc) + "; подготовленная копия для разбора: " + str(stage)) from exc
    _verify_tree(repo, result["head"], plan["payload"])
    if action == "push":
        _check_clean(repo)
        if _text(repo, "symbolic-ref", "--short", "HEAD") != "main" or _text(repo, "rev-parse", "HEAD") != result["head"]:
            raise PublishError("активная main изменилась перед отправкой")
        latest = _remote(repo, remote)
        if latest not in (plan["remote_head"], result["head"]):
            raise PublishError("серверная main изменилась после проверки; локальный коммит сохранён, отправка отменена")
        if latest != result["head"] and git(repo, "merge-base", "--is-ancestor", latest, result["head"], check=False).returncode:
            raise PublishError("серверная main изменилась; готовый локальный коммит сохранён, отправка отменена")
        _check_ahead_history(repo, latest, result["head"], plan["payload"])
        git(repo, "push", "origin", "refs/heads/main:refs/heads/main")
        remote_line = _text(repo, "ls-remote", "--heads", "origin", "refs/heads/main").split()
        if not remote_line or remote_line[0] != result["head"]:
            raise PublishError("после отправки main на сервере не совпала; локальный коммит сохранён")
        result["pushed"] = True
    return result


def probe_bundle(bundle, book, home, output, downloads=None, yandex=None, no_zip=False):
    plan, project, issues = [], None, []
    try:
        manifest = read_manifest(bundle)
        plan = make_plan(bundle, manifest, book, home, downloads)
        project = check_project(book)
        unknown = [item["target"] for item in plan if not item["compatible"]]
        if unknown:
            issues.append("неизвестные локальные редакции кода: " + "; ".join(unknown))
        pending = unfinished_updates(home)
        if pending:
            issues.append("есть незавершённое обновление: " + str(pending[0]))
        active = running_writers(book, home)
        if active:
            issues.append("перед обновлением штатно завершите NOX: " + ", ".join(str(p["pid"]) + " " + p["name"] for p in active))
        print("Комплект: " + manifest["product_version"] + " / " + manifest["build_id"])
        current = status(book)
        print("Фраз: " + str(current.get("phrases", 0)) + "; закреплено частей: " + str(current.get("fixed_parts", 0)))
        print("Изменяемых файлов кода: " + str(sum(item["changed"] for item in plan)))
    except (UpdateError, OSError, ValueError, KeyError) as exc:
        issues.append(str(exc))
    if yandex is not None:
        print("Яндекс-папка: " + str(yandex) + (" (доступна)" if yandex.is_dir() else " (не найдена по этому пути)"))
    for issue in issues:
        print("СТОП обновления: " + issue)
    if issues and not no_zip:
        print("Диагностика без аудио: " + str(save_diagnostics(output, book, home, plan, issues, project)))
    print("Проверка закончена. Книга, установленный код и процессы не изменялись.")
    return 2 if issues else 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--bundle", type=Path, required=True)
    probe.add_argument("--root", type=Path, required=True)
    probe.add_argument("--home", type=Path, required=True)
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--downloads", type=Path)
    probe.add_argument("--yandex", type=Path)
    probe.add_argument("--no-zip", action="store_true")
    publication = sub.add_parser("publish")
    publication.add_argument("--bundle", type=Path, required=True)
    publication.add_argument("--dir", type=Path, required=True)
    publication.add_argument("--repo", required=True)
    publication.add_argument("--book", type=Path)
    publication.add_argument("--home", type=Path)
    publication.add_argument("--message")
    publication.add_argument("--action", choices=("plan", "prepare", "push"), default="plan")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            return probe_bundle(args.bundle.resolve(), args.root.resolve(), args.home.resolve(), args.output.resolve(),
                                args.downloads.resolve() if args.downloads else None, args.yandex, args.no_zip)
        value = publish_bundle(args.bundle, args.dir, args.repo, action=args.action, book=args.book,
                               home=args.home, message=args.message)
        print("Файлов в плане: " + str(len(value["changed"])))
        for name in value["changed"]:
            print("  " + name)
        print("Коммит main: " + value["head"])
        print("Отправлено в main." if value["pushed"] else
              "Локальный коммит подготовлен; отправки не было." if args.action == "prepare" else
              "Показан план. Для локальной подготовки нужен -NoPush; для отправки — явный -Push.")
        return 0
    except (PublishError, UpdateError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("СТОП: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
