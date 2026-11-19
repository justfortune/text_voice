"""Failure probes of the actual 18d updater/publication helpers.

No real book, GPU, services or Windows installation are used. PS5.1 checks here
are static checks only; they do not substitute for running that interpreter.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

KIT = Path(__file__).resolve().parents[1] / "kit"
sys.path.insert(0, str(KIT))
import nox_update as update
import nox_release_io as release


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle, self.book, self.home = (self.root / n for n in ("bundle", "book", "home"))
        for p in (self.bundle / "kit", self.book / "work", self.book / "studio", self.home / "studio"):
            p.mkdir(parents=True)
        (self.book / "work/segments.csv").write_bytes(b'id;text;para;chapter\n1;A sentence.;1;1\n')
        (self.book / "studio/edits.json").write_text('{"parts":[],"segs":{}}')
        self.old = {"a.py": b"\xef\xbb\xbf# old A\r\nprint('A')\r\n", "b.py": b"# old B\r\nprint('B')\r\n"}
        self.new = {"a.py": b"# new A\nprint('18d A')\n", "b.py": b"# new B\nprint('18d B')\n"}
        self.manifest = {"format": 1, "product_version": "2026-11-18d", "build_id": "test18d", "base_commit": "fixture", "files": [], "targets": []}
        for name, data in self.new.items():
            (self.bundle / "kit" / name).write_bytes(data)
            (self.book / name).write_bytes(self.old[name])
            self.manifest["files"].append({"source": "kit/" + name, "sha256": sha(data)})
            self.manifest["targets"].append({"source": "kit/" + name, "scope": "book", "path": name, "known_previous": [update.normalized_hash(self.old[name])]})
        self.save_manifest()

    def save_manifest(self):
        (self.bundle / "release_manifest.json").write_text(json.dumps(self.manifest))

    def plan(self):
        m = update.read_manifest(self.bundle)
        return update.make_plan(self.bundle, m, self.book, self.home), update.check_project(self.book)

    def install(self):
        plan, project = self.plan()
        return update.install(self.bundle, self.manifest, self.book, self.home, plan, project)


class InstallerTests(Fixture):
    def test_unknown_hash_stops_before_any_destination_write(self):
        (self.book / "b.py").write_bytes(b"user customised code\n")
        plan, project = self.plan()
        before_book, before_home = inventory(self.book), inventory(self.home)
        self.assertFalse(next(x for x in plan if x["relative"] == "b.py")["compatible"])
        with self.assertRaises(update.UpdateError):
            update.install(self.bundle, self.manifest, self.book, self.home, plan, project)
        self.assertEqual(inventory(self.book), before_book)
        self.assertEqual(inventory(self.home), before_home)

    def test_manifest_source_and_target_traversal_rejected(self):
        for bad in ("../outside.py", "/outside.py", "dir/../../outside.py", "C:/outside.py", "dir\\outside.py"):
            with self.subTest(path=bad):
                m = json.loads(json.dumps(self.manifest))
                m["files"][0]["source"] = bad
                (self.bundle / "release_manifest.json").write_text(json.dumps(m))
                with self.assertRaises(update.UpdateError):
                    update.read_manifest(self.bundle)
                m = json.loads(json.dumps(self.manifest))
                m["targets"][0]["path"] = bad
                with self.assertRaises(update.UpdateError):
                    update.make_plan(self.bundle, m, self.book, self.home)

    def test_manifest_bad_hash_and_duplicate_destination_rejected(self):
        (self.bundle / "kit/a.py").write_bytes(b"modified payload")
        with self.assertRaises(update.UpdateError):
            update.read_manifest(self.bundle)
        (self.bundle / "kit/a.py").write_bytes(self.new["a.py"])
        m = json.loads(json.dumps(self.manifest)); m["targets"].append(dict(m["targets"][0], path="A.PY"))
        with self.assertRaises(update.UpdateError):
            update.make_plan(self.bundle, m, self.book, self.home)

    def test_symlink_parent_cannot_escape_book(self):
        outside = self.root / "outside"; outside.mkdir()
        try:
            (self.book / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable on this test host")
        with self.assertRaises(update.UpdateError):
            update.child(self.book, "linked/code.py")

    def test_lf_crlf_and_utf8_bom_known_previous_are_compatible(self):
        canonical = b"# sample\nprint('x')\n"
        variants = (canonical, canonical.replace(b"\n", b"\r\n"), b"\xef\xbb\xbf" + canonical, b"\xef\xbb\xbf" + canonical.replace(b"\n", b"\r\n"))
        self.assertEqual({update.normalized_hash(x) for x in variants}, {sha(canonical)})
        plan, _ = self.plan()
        self.assertTrue(all(x["compatible"] for x in plan))

    def test_check_is_read_only_and_metadata_hashes_stable(self):
        before_book, before_home, hashes = inventory(self.book), inventory(self.home), update.state_hashes(self.book)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = update.main(["--root", str(self.book), "--home", str(self.home), "--bundle", str(self.bundle), "--output", str(self.root / "reports")])
        self.assertEqual(rc, 0)
        self.assertEqual(inventory(self.book), before_book)
        self.assertEqual(inventory(self.home), before_home)
        self.assertEqual(update.state_hashes(self.book), hashes)

    def test_second_code_write_failure_rolls_back_exact_original_bytes(self):
        plan, project = self.plan(); original = update.atomic_copy_code; failed = False
        def fail_second(source, destination, expected):
            nonlocal failed
            if Path(destination) == self.book / "b.py" and not failed:
                failed = True
                raise OSError("injected second code write failure")
            return original(source, destination, expected)
        before_book = inventory(self.book)
        with mock.patch.object(update, "atomic_copy_code", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                update.install(self.bundle, self.manifest, self.book, self.home, plan, project)
        self.assertTrue(failed)
        self.assertEqual(inventory(self.book), before_book)
        journal = next((self.home / "update_backups").glob("*/update.json"))
        self.assertEqual(json.loads(journal.read_text())["state"], "rolled_back")

    def test_corrupt_backup_stops_before_first_restoration(self):
        journal = self.install(); doc = json.loads(journal.read_text())
        existing = next(x for x in doc["items"] if x["old_hash"] is not None)
        (journal.parent / existing["backup"]).write_bytes(b"corrupt")
        before_book, before_app = inventory(self.book), inventory(self.home / "studio")
        with self.assertRaises(update.UpdateError):
            update.restore_code(journal, expected_book=self.book, expected_home=self.home)
        self.assertEqual(inventory(self.book), before_book)
        self.assertEqual(inventory(self.home / "studio"), before_app)

    def test_rollback_refuses_changed_book_metadata(self):
        journal = self.install()
        (self.book / "work/segments.csv").write_text("text\nChanged\n")
        before = inventory(self.book)
        with self.assertRaises(update.UpdateError):
            update.restore_code(journal, expected_book=self.book, expected_home=self.home)
        self.assertEqual(inventory(self.book), before)

    def test_rollback_rejects_target_outside_declared_scope(self):
        journal = self.install(); doc = json.loads(journal.read_text())
        outside = self.root / "outside.py"; outside.write_bytes(self.new["a.py"])
        doc["items"][0]["target"] = str(outside)
        journal.write_text(json.dumps(doc)); before = inventory(self.book)
        with self.assertRaises(update.UpdateError):
            update.restore_code(journal, expected_book=self.book, expected_home=self.home)
        self.assertEqual(inventory(self.book), before)
        self.assertEqual(outside.read_bytes(), self.new["a.py"])

    def test_rollback_cli_binds_journal_to_requested_book(self):
        journal = self.install(); before = inventory(self.book)
        other = self.root / "otherbook"; other.mkdir()
        # Only the updater's platform guard is replaced. pathlib and filesystem
        # remain the actual test host; no Windows APIs are pretended to run.
        with mock.patch.object(update, "os", SimpleNamespace(name="nt")), mock.patch.object(update, "running_writers", return_value=[]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = update.main(["--root", str(other), "--home", str(self.home), "--rollback", str(journal), "--output", str(self.root / "reports")])
        self.assertEqual(rc, 2, "journal from another book must be rejected before restoring anything")
        self.assertEqual(inventory(self.book), before)

    def test_process_root_boundary_matches_only_same_book(self):
        book = PureWindowsPath(r"D:\kniga1a")
        for command in (r'python "D:\kniga1a\nox_fast.py"', r'python D:/KNIGA1A/nox_fast.py', r'powershell -Root="D:\kniga1a"'):
            self.assertTrue(update.process_uses_path(command, book), command)
        for command in (r'python "D:\kniga1a_old\nox_fast.py"', r'python D:\kniga1a2\nox_fast.py', r'python E:\kniga1a\nox_fast.py', r'python D:\books\kniga1a\nox_fast.py'):
            self.assertFalse(update.process_uses_path(command, book), command)

    def test_ps_wrapper_static_ascii_and_no_cleanup_or_package_mutation(self):
        ps = (KIT.parent / "Update_NOX_18d.ps1").read_bytes().decode("ascii")
        self.assertIn("PowerShell 5.1", ps)
        for forbidden in ("Remove-Item", "Stop-Process", "pip install", "Invoke-Expression", "&&", "??"):
            self.assertNotIn(forbidden, ps)
        self.assertIn("'--apply'", ps); self.assertIn("'--rollback'", ps)


class PublicationTests(Fixture):
    def publication(self):
        stage = self.root / "stage"; stage.mkdir()
        pairs = []
        for name in ("a.py", "b.py"):
            src = stage / name; src.write_bytes(self.new[name]); pairs.append((src, self.book / name))
        return release.Publication(self.book / "journals", pairs, allowed_root=self.book)

    def test_nth_rename_failure_restores_all_old_files(self):
        tx = self.publication(); original = release.os.replace; failed = False
        def injected(source, destination):
            nonlocal failed
            if Path(source) == self.root / "stage/b.py" and not failed:
                failed = True; raise OSError("injected publish failure")
            return original(source, destination)
        with mock.patch.object(release.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(OSError, "injected"):
                tx.publish()
        for name, raw in self.old.items(): self.assertEqual((self.book / name).read_bytes(), raw)
        self.assertEqual(tx.doc["state"], "rolled_back")

    def test_changed_staged_source_never_replaces_old_output(self):
        tx = self.publication()
        (self.root / "stage/a.py").write_bytes(b"changed since preparation")
        with self.assertRaises(release.PublicationError):
            tx.publish()
        for name, raw in self.old.items(): self.assertEqual((self.book / name).read_bytes(), raw)

    def test_published_journal_failure_restores_old_group(self):
        tx = self.publication(); original = tx._save; failed = False
        def injected():
            nonlocal failed
            if tx.doc["state"] == "published" and not failed:
                failed = True; raise OSError("injected journal failure")
            return original()
        with mock.patch.object(tx, "_save", side_effect=injected):
            with self.assertRaisesRegex(OSError, "injected"):
                tx.publish()
        for name, raw in self.old.items(): self.assertEqual((self.book / name).read_bytes(), raw)

    def test_recover_interrupted_published_group(self):
        tx = self.publication(); tx.publish()
        recovered = release.recover_publications(self.book / "journals", self.book)
        self.assertEqual(recovered, [str(tx.path)])
        for name, raw in self.old.items(): self.assertEqual((self.book / name).read_bytes(), raw)

    def test_failed_commit_journal_retains_rollback_state_and_backups(self):
        tx = self.publication(); tx.publish()
        original = tx._save
        def injected():
            if tx.doc["state"] == "committed": raise OSError("commit journal failed")
            return original()
        with mock.patch.object(tx, "_save", side_effect=injected):
            with self.assertRaisesRegex(OSError, "commit journal"):
                tx.commit()
        self.assertEqual(tx.doc["state"], "published")
        tx.rollback()
        for name, raw in self.old.items(): self.assertEqual((self.book / name).read_bytes(), raw)

    def test_corrupt_group_backup_stops_before_any_restoration(self):
        tx = self.publication(); tx.publish()
        Path(tx.doc["items"][0]["backup"]).write_bytes(b"bad backup")
        before = {name:(self.book / name).read_bytes() for name in self.old}
        with self.assertRaises(release.PublicationError): tx.rollback()
        self.assertEqual({name:(self.book / name).read_bytes() for name in self.old}, before)

    def test_atomic_write_failure_keeps_destination_and_removes_temp(self):
        target = self.book / "a.py"; before = target.read_bytes()
        with mock.patch.object(release.os, "replace", side_effect=PermissionError("injected")):
            with self.assertRaises(PermissionError): release.atomic_write(target, b"new", attempts=1)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.book.glob(".a.py.*.tmp")), [])

    def test_concurrent_atomic_writes_never_mix_or_reuse_temp(self):
        target = self.book / "atomic.bin"; values = [bytes([i]) * (8192 + i) for i in range(24)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda data: release.atomic_write(target, data), values))
        self.assertIn(target.read_bytes(), values)
        self.assertEqual(list(self.book.glob(".atomic.bin.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
