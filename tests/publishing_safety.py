"""Real Git acceptance tests; remotes are local TemporaryDirectory fixtures."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import nox_bundle_tools as pub


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "checkout"; self.repo.mkdir()
        self.remote = self.root / "origin.git"
        self.command("git", "init", "--bare", "-b", "main", str(self.remote))
        self.command("git", "init", "-b", "main", str(self.repo))
        self.g("config", "user.name", "Fixture")
        self.g("config", "user.email", "fixture@users.noreply.github.com")
        (self.repo / "kit").mkdir()
        self.initial = {"README.md": b"Initial code\n", "kit/app.py": b"VERSION = '12b'\n", ".gitignore": b"__pycache__/\n"}
        for name, data in self.initial.items(): (self.repo / name).write_bytes(data)
        self.g("add", "README.md", "kit/app.py", ".gitignore")
        self.g("commit", "-m", "initial main")
        self.base = self.g("rev-parse", "HEAD")
        self.g("remote", "add", "origin", str(self.remote))
        self.g("push", "origin", "main")
        self.bundle = self.root / "release"; (self.bundle / "kit").mkdir(parents=True)
        self.payload = dict(self.initial, **{"README.md": b"NOX 18d release\n", "kit/app.py": b"VERSION = '18d'\n"})
        for name, data in self.payload.items(): (self.bundle / name).write_bytes(data)
        self.manifest = {"format": 1, "product_version": "2026-11-18d", "build_id": "fixture.18d", "base_commit": self.base,
                         "files": [{"source": name, "sha256": pub.digest(self.bundle / name)} for name in self.payload],
                         "targets": []}
        self.write_manifest()

    def command(self, *args):
        result = subprocess.run(list(args), capture_output=True, check=True)
        return result.stdout.decode().strip()

    def g(self, *args):
        return self.command("git", "-C", str(self.repo), *args)

    def write_manifest(self):
        (self.bundle / "release_manifest.json").write_text(json.dumps(self.manifest))

    def publish(self, action="plan"):
        with contextlib.redirect_stdout(io.StringIO()):
            return pub.publish_bundle(self.bundle, self.repo, str(self.remote), action=action,
                                      book=self.root / "book", home=self.root / "environment")

    def test_default_plan_changes_no_book_or_checkout_files(self):
        result = self.publish()
        self.assertFalse(result["pushed"])
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.g("status", "--porcelain"), "")
        for name, data in self.initial.items(): self.assertEqual((self.repo / name).read_bytes(), data)

    def test_prepare_extends_main_then_explicit_push_is_fast_forward(self):
        self.g("branch", "untouched")
        result = self.publish("prepare")
        self.assertFalse(result["pushed"])
        self.assertNotEqual(result["head"], self.base)
        self.assertEqual(self.g("rev-parse", "HEAD^"), self.base)
        self.assertEqual(self.g("rev-parse", "untouched"), self.base)
        self.assertEqual(self.g("symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.command("git", "--git-dir", str(self.remote), "rev-parse", "main"), self.base)
        result = self.publish("push")
        self.assertTrue(result["pushed"])
        self.assertEqual(self.command("git", "--git-dir", str(self.remote), "rev-parse", "main"), result["head"])
        self.assertEqual(self.g("status", "--porcelain"), "")

    def test_dirty_local_code_is_preserved(self):
        path = self.repo / "README.md"; path.write_text("my unsaved edits")
        with self.assertRaises(pub.PublishError): self.publish("prepare")
        self.assertEqual(path.read_text(), "my unsaved edits")
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)

    def test_unknown_committed_local_changes_are_not_replaced(self):
        path = self.repo / "kit/app.py"; path.write_text("LOCAL_CUSTOM_CHANGE = 1\n")
        self.g("add", "kit/app.py"); self.g("commit", "-m", "custom change")
        head = self.g("rev-parse", "HEAD")
        with self.assertRaises(pub.PublishError): self.publish("prepare")
        self.assertEqual(path.read_text(), "LOCAL_CUSTOM_CHANGE = 1\n")
        self.assertEqual(self.g("rev-parse", "HEAD"), head)

    def test_remote_advance_is_reported_without_reset_merge_or_push(self):
        other = self.root / "other"
        self.command("git", "clone", str(self.remote), str(other))
        self.command("git", "-C", str(other), "config", "user.name", "Fixture")
        self.command("git", "-C", str(other), "config", "user.email", "fixture@users.noreply.github.com")
        (other / "README.md").write_text("New remote main\n")
        self.command("git", "-C", str(other), "add", "README.md")
        self.command("git", "-C", str(other), "commit", "-m", "remote advance")
        self.command("git", "-C", str(other), "push", "origin", "main")
        with self.assertRaisesRegex(pub.PublishError, "сервере продвинулась"):
            self.publish("push")
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)
        self.assertEqual((self.repo / "README.md").read_bytes(), self.initial["README.md"])

    def test_other_active_branch_is_not_renamed_or_used(self):
        self.g("switch", "-c", "abandoned")
        with self.assertRaises(pub.PublishError): self.publish("prepare")
        self.assertEqual(self.g("symbolic-ref", "--short", "HEAD"), "abandoned")
        self.assertEqual(self.g("rev-parse", "main"), self.base)

    def test_private_transfer_identifiers_in_bundle_are_rejected(self):
        path = self.bundle / "README.md"
        path.write_text("private " + "file_" + "a" * 32)
        self.manifest["files"][0]["sha256"] = pub.digest(path); self.write_manifest()
        with self.assertRaisesRegex(pub.PublishError, "личного переноса"):
            self.publish("prepare")
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)

    def test_deleted_private_file_in_unpublished_history_also_blocks_push(self):
        (self.repo / "private.txt").write_text("local data")
        self.g("add", "private.txt"); self.g("commit", "-m", "private file")
        self.g("rm", "private.txt"); self.g("commit", "-m", "remove private file")
        head = self.g("rev-parse", "HEAD")
        with self.assertRaisesRegex(pub.PublishError, "вне manifest"):
            self.publish("push")
        self.assertEqual(self.g("rev-parse", "HEAD"), head)
        self.assertEqual(self.command("git", "--git-dir", str(self.remote), "rev-parse", "main"), self.base)

    def test_copy_failure_never_changes_user_main_checkout(self):
        with patch.object(pub, "atomic_write", side_effect=PermissionError("injected")):
            with self.assertRaises(pub.PublishError): self.publish("prepare")
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.g("status", "--porcelain"), "")
        for name, data in self.initial.items(): self.assertEqual((self.repo / name).read_bytes(), data)

    def test_manifest_hash_mismatch_stops_before_git_worktree(self):
        (self.bundle / "kit/app.py").write_text("damaged")
        with self.assertRaises(pub.UpdateError): self.publish("prepare")
        self.assertEqual(self.g("rev-parse", "HEAD"), self.base)

    def test_nozip_probe_checks_unknown_code_without_writes(self):
        book = self.root / "book"; (book / "work").mkdir(parents=True)
        table = book / "work/segments.csv"; table.write_text("id;chapter;para;text\n1;1;1;Фраза.\n", encoding="utf-8")
        installed = book / "nox_demo.py"; installed.write_text("custom local code")
        self.manifest["targets"] = [{"source": "kit/app.py", "scope": "book", "path": "nox_demo.py", "known_previous": []}]
        self.write_manifest()
        with patch.object(pub, "save_diagnostics") as diagnostics, contextlib.redirect_stdout(io.StringIO()):
            result = pub.probe_bundle(self.bundle, book, self.root / "home", self.root / "output", no_zip=True)
        self.assertEqual(result, 2); diagnostics.assert_not_called()
        self.assertEqual(installed.read_text(), "custom local code")
        self.assertEqual(table.read_text(), "id;chapter;para;text\n1;1;1;Фраза.\n")
        self.assertFalse((self.root / "output").exists())


class PowershellSourceTests(unittest.TestCase):
    def test_wrappers_are_ps51_compatible_sources_with_required_options(self):
        root = Path(__file__).resolve().parents[1] / "tools"
        names = {"sborshchik_k1a.ps1": ("$Root", "$Src", "$Downloads", "$Yandex", "$Fix", "$NoZip"),
                 "v_github.ps1": ("$Repo", "$Dir", "$Book", "$Src", "$Downloads", "$NoPush", "$Push")}
        for name, options in names.items():
            raw = (root / name).read_bytes(); text = raw.decode("utf-8-sig")
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            for option in options: self.assertIn(option, text)
            for unsupported in ("??", "&&", "||", "-AsHashtable", "-Parallel"):
                self.assertNotIn(unsupported, text)
            for unsafe in ("Remove-Item", "Move-Item", "Stop-Process", "reset --", "branch -M", "--force", "add -A"):
                self.assertNotIn(unsafe, text)


if __name__ == "__main__":
    unittest.main()
