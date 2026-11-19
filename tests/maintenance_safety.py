"""Actual maintenance integration, fault injection and protection contracts.

Fixtures contain no real book. WAV bytes are opaque transaction payloads; the
tests do not pretend to run synthesis or Windows PowerShell on Linux.
"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import errno
import io
import json
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest import mock

KIT = Path(__file__).resolve().parents[1] / "kit"
sys.path.insert(0, str(KIT))
import nox_maintenance as m
import nox_file_transactions as ft
from nox_freeze import ProtectionError, fixed_document, row_uids


def no_processes():
    return []


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = Path(self.temp.name) / "book"
        self.book.mkdir()
        self.rows = [{"id": "1", "text": "A", "para": "1", "chapter": "1"},
                     {"id": "2", "text": "B", "para": "2", "chapter": "1"}]
        self.uids = row_uids(self.rows)
        self.put("book.docx", b"manuscript")
        self.put("work/segments.csv", b"id;text;para;chapter\r\n1;A;1;1\r\n2;B;2;1\r\n")
        self.put_json("work/parts.json", {"parts": []})
        self.put_json("work/assembly.json", {"speed": 1.12})
        self.put_json("work/roles.json", {"narrator": {"ref": "narrator.wav"}})
        self.put("work/voices/narrator.wav", b"voice sample")
        self.put("work/voices_old/v.wav", b"old voice sample")
        self.put("work/wav/A.wav", b"pipeline wave A")
        self.put("work/wav/A.json", b'{"raw":"A","role":"narrator"}')
        self.put("work/wav/A.txt", b"A technical text")
        self.put("work/wav/B.wav", b"pipeline wave B")
        self.put("work/wav/B.json", b'{"raw":"B","role":"narrator"}')
        self.put("work/asr_log.txt", b"verification result")
        self.put_json("work/audio_history/old/manifest.json", {"format": 1, "kind": "pipeline_audio", "state": "archived", "files": []})
        self.put("work/freeze_backups/old/segments.csv", b"old immutable CSV")
        self.put("work/retained_audio/old.wav", b"retained audio")
        self.put("work/input_history/old/book.docx", b"previous input")
        self.put("work/voice_history/samples/old.wav", b"previous role voice")
        self.put("work/nox_release.json", b"release marker")
        self.put_json("studio/edits.json", {"parts": [], "segs": {}, "cues": []})
        self.put_json("studio/fixed_parts.json", {"ver": 1, "parts": []})
        self.put("studio/takes/aaa/aaa_t001.wav", b"studio WAV")
        self.put("studio/takes/aaa/aaa_t001.json", b'{"tid":"t001","raw":"A"}')
        self.put("studio/trash/old/aaa_t000.wav", b"Undo still needs me")
        self.put("studio/trash/old/aaa_t000.json", b'{"tid":"t000"}')
        self.put_json("studio/transactions/" + "a" * 32 + "/operation.json", {
            "schema": 1, "id": "a" * 32, "state": "committed", "moves": [], "documents": [], "at": 1})
        self.put("studio/projects/author.noxproj", b"saved project refers to those takes")
        self.put("studio/sounds/bell.wav", b"user sound")
        self.put("studio/imports/rain.mp3", b"import")
        self.put("studio/home.id", b"book identity")
        self.put_json("studio/sound_roots.json", {"roots": []})
        self.put("parts/part_01.mp3", b"pipeline assembled part")
        self.put("gotovo/part_01.mp3", b"author export")
        self.put("nox_fast.py", b"installed worker code")
        self.put("envfast/pyvenv.cfg", b"env remains")
        self.put("hf/model.bin", b"weights remain")

    def put(self, rel, data):
        p = self.book / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def put_json(self, rel, data):
        return self.put(rel, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def plan(self, mode="edition", no_backup=False):
        return m.build_plan(self.book, mode, no_backup=no_backup, process_reader=no_processes)

    def apply(self, mode="edition", no_backup=False):
        return m.apply_plan(self.book, self.plan(mode, no_backup), process_reader=no_processes)

    def restore(self, op_id):
        return m.restore(self.book, op_id, process_reader=no_processes)

    def recover(self):
        return m.recover(self.book, process_reader=no_processes)

    def assert_originals(self, expected):
        for rel, content in expected.items():
            self.assertEqual((self.book / rel).read_bytes(), content, rel)


class MaintenanceIntegration(Fixture):
    def test_default_plan_is_completely_read_only(self):
        before = inventory(self.book)
        plan = self.plan()
        self.assertGreater(plan["bytes"], 0)
        self.assertFalse((self.book / "maintenance").exists())
        self.assertEqual(inventory(self.book), before)

    def test_pipeline_preserves_csv_studio_exports_code_and_every_history(self):
        before = inventory(self.book)
        plan = self.plan("pipeline")
        moved = {f["source"] for f in plan["files"]}
        self.assertIn("work/wav/A.wav", moved)
        self.assertIn("work/wav/A.json", moved)
        for rel in before:
            if rel.startswith(("studio/", "gotovo/", "work/audio_history/", "work/freeze_backups/",
                               "work/retained_audio/", "work/voice_history/", "work/input_history/", "work/voices", "envfast/", "hf/")) or rel in (
                    "work/segments.csv", "work/parts.json", "work/assembly.json", "nox_fast.py"):
                self.assertNotIn(rel, moved, rel)
        result = m.apply_plan(self.book, plan, process_reader=no_processes)
        self.assertEqual(result["freed_bytes"], 0)
        for rel in before.keys() - moved:
            self.assertEqual((self.book / rel).read_bytes(), before[rel])
        self.restore(result["id"])
        self.assert_originals(before)

    def test_edition_moves_complete_undo_project_and_audio_bundles_then_restores_exact_bytes(self):
        before = inventory(self.book)
        result = self.apply()
        manifest = m._manifest(self.book, result["id"])
        sources = {f["source"] for f in manifest["files"]}
        for required in ("work/segments.csv", "studio/edits.json", "studio/takes/aaa/aaa_t001.wav",
                         "studio/takes/aaa/aaa_t001.json", "studio/trash/old/aaa_t000.wav",
                         "studio/projects/author.noxproj", "studio/transactions/" + "a" * 32 + "/operation.json"):
            self.assertIn(required, sources)
            self.assertFalse((self.book / required).exists())
        for f in manifest["files"]:
            self.assertEqual((self.book / f["target"]).read_bytes(), before[f["source"]])
        self.assertTrue((self.book / "studio/sounds/bell.wav").exists())
        self.assertTrue((self.book / "studio/imports/rain.mp3").exists())
        self.assertTrue((self.book / "studio/home.id").exists())
        restored = self.restore(result["id"])
        self.assertEqual(restored["state"], "restored")
        self.assert_originals(before)
        self.assertEqual(self.restore(result["id"])["state"], "already_restored")

    def test_no_backup_skips_only_extra_inputs_and_still_preserves_every_removed_byte(self):
        before = inventory(self.book)
        plan = self.plan(no_backup=True)
        self.assertEqual(plan["snapshots"], [])
        result = m.apply_plan(self.book, plan, process_reader=no_processes)
        folder = self.book / "maintenance/quarantine" / result["id"]
        self.assertFalse((folder / "inputs").exists())
        self.assertTrue((folder / "files/work/wav/A.wav").is_file())
        self.restore(result["id"])
        self.assert_originals(before)

    def test_extra_metadata_snapshot_has_no_duplicate_audio(self):
        result = self.apply()
        folder = self.book / "maintenance/quarantine" / result["id"]
        self.assertEqual((folder / "inputs/book.docx").read_bytes(), b"manuscript")
        self.assertEqual(list((folder / "inputs").rglob("*.wav")), [])
        self.assertFalse((self.book / "work/wav/A.wav").exists())

    def test_changed_file_new_file_or_fabricated_plan_fails_before_writes(self):
        for change in ("changed", "new", "forged"):
            with self.subTest(change=change):
                plan = self.plan()
                if change == "changed":
                    self.put("work/wav/A.wav", b"new speech")
                elif change == "new":
                    self.put("work/wav/C.wav", b"new file")
                else:
                    plan["files"].append({"source": "book.docx", "sha256": m.digest(self.book / "book.docx"), "size": 10})
                before = inventory(self.book)
                with self.assertRaises(m.MaintenanceError):
                    m.apply_plan(self.book, plan, process_reader=no_processes)
                self.assertEqual(inventory(self.book), before)

    def test_opened_unlocked_part_is_protection_too(self):
        self.put_json("studio/edits.json", {"parts": [{"n": 1, "last": self.uids[0]}], "segs": {}})
        before = inventory(self.book)
        for no_backup in (False, True):
            with self.assertRaisesRegex(m.MaintenanceError, "открытые"):
                self.plan(no_backup=no_backup)
        self.assertEqual(inventory(self.book), before)

    def test_individual_lock_and_consistent_full_part_lock_refuse(self):
        edits = {"parts": [], "segs": {self.uids[0]: {"take": "t001", "locked": {"take": "t001"}}}}
        self.put_json("studio/edits.json", edits)
        with self.assertRaises(m.MaintenanceError):
            self.plan()
        edits["parts"] = [{"n": 1, "last": self.uids[0], "locked": "2026-11-19"}]
        self.put_json("studio/edits.json", edits)
        self.put_json("studio/fixed_parts.json", fixed_document(self.rows, edits))
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            self.plan(no_backup=True)
        self.assertEqual(inventory(self.book), before)

    def test_corrupt_missing_partner_and_disagreeing_protection_refuse(self):
        for data in (b"{", b'{"parts": {}}', b'{"ver":1,"parts":[{"n":1,"a":0,"b":0,"a_uid":"wrong","b_uid":"wrong"}]}'):
            with self.subTest(data=data):
                self.put("studio/fixed_parts.json", data)
                before = inventory(self.book)
                with self.assertRaises((m.MaintenanceError, ProtectionError)):
                    self.plan()
                self.assertEqual(inventory(self.book), before)
        self.put_json("studio/fixed_parts.json", {"ver": 1, "parts": []})
        self.put("studio/edits.json", b'{"parts":[],"segs":')
        with self.assertRaises(ProtectionError):
            self.plan()
        edits = {"parts": [{"n": 1, "last": self.uids[0], "locked": "2026-11-19"}],
                 "segs": {self.uids[0]: {"take": "t001", "locked": {"take": "t001"}}}}
        self.put_json("studio/fixed_parts.json", fixed_document(self.rows, edits))
        (self.book / "studio/edits.json").unlink()
        with self.assertRaises(ProtectionError):
            self.plan()

    def test_missing_csv_with_opened_boundary_and_truncated_csv_refuse(self):
        self.put_json("studio/edits.json", {"parts": [{"last": self.uids[0]}], "segs": {}})
        (self.book / "work/segments.csv").unlink()
        with self.assertRaises(ProtectionError):
            self.plan()
        self.put_json("studio/edits.json", {"parts": [], "segs": {}})
        self.put("work/segments.csv", b"id;text;para\n1;A\n")
        with self.assertRaises(ProtectionError):
            self.plan()

    def test_active_process_exact_root_refuses_other_book_does_not(self):
        def other():
            return [{"ProcessId": 999991, "Name": "python.exe", "CommandLine": f'python "{self.book}_old/nox_fast.py"'}]
        m.build_plan(self.book, process_reader=other)
        def current():
            return [{"ProcessId": 999992, "Name": "python.exe", "CommandLine": f'python "{self.book}/nox_fast.py"'}]
        before = inventory(self.book)
        with self.assertRaisesRegex(m.MaintenanceError, "PID 999992"):
            m.build_plan(self.book, process_reader=current)
        self.assertEqual(inventory(self.book), before)
        self.assertTrue(m.process_uses_path('python "D:\\kniga1a\\nox_fast.py"', PureWindowsPath(r"D:\kniga1a")))
        self.assertFalse(m.process_uses_path('python "D:\\kniga1a_old\\nox_fast.py"', PureWindowsPath(r"D:\kniga1a")))

    def test_pid_records_detect_relative_commands_and_fresh_verifier_heartbeat(self):
        self.put_json("studio/studio.lock", {"pid": 999993, "port": 8765})
        def relative():
            return [{"ProcessId": 999993, "Name": "python.exe", "CommandLine": "python nox_studio.py"}]
        with self.assertRaises(m.MaintenanceError):
            m.build_plan(self.book, process_reader=relative)
        (self.book / "studio/studio.lock").unlink()
        self.put("work/studio_queue/alive_w9.txt", b"999993\t100\tserve")
        with self.assertRaises(m.MaintenanceError):
            m.build_plan(self.book, process_reader=relative)
        (self.book / "work/studio_queue/alive_w9.txt").unlink()
        self.put("work/asr_active.txt", b"still verifying")
        with self.assertRaisesRegex(m.MaintenanceError, "asr_active"):
            self.plan()

    def test_own_apply_rechecks_process_before_first_move(self):
        plan = self.plan()
        count = 0
        def process_reader():
            nonlocal count
            count += 1
            return [] if count < 2 else [{"ProcessId": 999995, "Name": "python.exe", "CommandLine": f'python "{self.book}/nox_fast.py"'}]
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            m.apply_plan(self.book, plan, process_reader=process_reader)
        self.assert_originals(before)
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())

    def test_symlink_in_selected_files_and_restore_traversal_refuse(self):
        outside = Path(self.temp.name) / "outside.wav"
        outside.write_bytes(b"do not touch")
        try:
            (self.book / "work/wav/link.wav").symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(m.MaintenanceError):
            self.plan()
        (self.book / "work/wav/link.wav").unlink()
        result = self.apply()
        manifest_path = Path(result["manifest"])
        doc = json.loads(manifest_path.read_text())
        doc["files"][0]["source"] = "../outside.wav"
        manifest_path.write_text(json.dumps(doc))
        with self.assertRaises(m.MaintenanceError):
            self.restore(result["id"])
        self.assertEqual(outside.read_bytes(), b"do not touch")

    def test_pending_studio_file_or_export_transaction_blocks_cleanup(self):
        for rel in ("studio/transactions/" + "b" * 32 + "/operation.json", "studio/export_transactions/x.json"):
            self.put_json(rel, {"state": "prepared"})
            before = inventory(self.book)
            with self.assertRaisesRegex(m.MaintenanceError, "Studio"):
                self.plan()
            self.assertEqual(inventory(self.book), before)
            (self.book / rel).unlink()

    def test_pending_pipeline_archive_blocks_cleanup_of_remaining_bundle_files(self):
        self.put_json("work/audio_history/interrupted/manifest.json", {"format": 1, "kind": "pipeline_audio", "state": "prepared", "files": []})
        before = inventory(self.book)
        with self.assertRaisesRegex(m.MaintenanceError, "WAV"):
            self.plan()
        self.assertEqual(inventory(self.book), before)

    def test_nth_move_failure_restores_every_original_and_keeps_no_false_success(self):
        before = inventory(self.book)
        original = ft.os.rename
        calls = 0
        def fail(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise PermissionError("Windows sharing violation")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=fail):
            with self.assertRaises(ft.TransactionError):
                self.apply()
        self.assert_originals(before)
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())
        records = m._tx(self.book).history()
        self.assertEqual(records[-1]["state"], "rolled_back")

    def test_cross_volume_rename_has_no_copy_or_delete_fallback(self):
        before = inventory(self.book)
        with mock.patch.object(ft.os, "rename", side_effect=OSError(errno.EXDEV, "different volume")):
            with self.assertRaises(ft.TransactionError):
                self.apply(no_backup=True)
        self.assert_originals(before)

    def test_crash_between_bundle_files_is_durably_recovered(self):
        before = inventory(self.book)
        original = ft.os.rename
        def crash(source, target):
            if Path(source).name == "aaa_t001.wav":
                raise KeyboardInterrupt("simulated process death after its JSON moved")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertTrue((self.book / "work/.nox_prepare.lock").exists())
        self.assertFalse((self.book / "studio/takes/aaa/aaa_t001.json").exists())
        self.assertTrue((self.book / "studio/takes/aaa/aaa_t001.wav").exists())
        state = inventory(self.book)
        plan = m.recovery_plan(self.book, process_reader=no_processes)
        self.assertEqual(plan["state"], "recovery_available")
        self.assertEqual(inventory(self.book), state)
        result = self.recover()
        self.assertEqual(result["outcome"], "rolled_back")
        self.assert_originals(before)
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())

    def test_crash_immediately_after_marker_can_recover_without_touching_sources(self):
        before = inventory(self.book)
        with mock.patch.object(m, "atomic_json", side_effect=KeyboardInterrupt("before manifest write")):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertTrue((self.book / "work/.nox_prepare.lock").exists())
        self.recover()
        self.assert_originals(before)
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())

    def test_corrupt_file_after_crash_blocks_recovery_before_any_reverse_move(self):
        original = ft.os.rename
        def crash(source, target):
            if Path(source).name == "aaa_t001.wav":
                raise KeyboardInterrupt("between paired files")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        marker = json.loads((self.book / "work/.nox_prepare.lock").read_text())
        doc = m._manifest(self.book, marker["id"])
        moved = next(f for f in doc["files"] if (self.book / f["target"]).exists())
        (self.book / moved["target"]).write_bytes(b"damaged quarantine copy")
        before = inventory(self.book)
        with self.assertRaises(ft.TransactionError):
            self.recover()
        self.assertEqual(inventory(self.book), before)
        self.assertTrue((self.book / "work/.nox_prepare.lock").exists())

    def test_pending_restore_without_marker_is_not_mistaken_for_completed_cleanup(self):
        result = self.apply()
        original = ft.os.rename
        calls = 0
        def crash(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("interrupted restore")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(result["id"])
        (self.book / "work/.nox_prepare.lock").unlink()
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            self.plan()
        self.assertEqual(inventory(self.book), before)

    def test_crash_after_commit_finalizes_instead_of_undoing_success(self):
        with mock.patch.object(m, "_remove_marker", side_effect=KeyboardInterrupt("after commit")):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertFalse((self.book / "work/wav/A.wav").exists())
        result = self.recover()
        self.assertEqual(result["outcome"], "committed")
        self.assertFalse((self.book / "work/wav/A.wav").exists())
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())

    def test_corrupted_quarantine_stops_restore_before_any_file_is_returned(self):
        result = self.apply()
        doc = m._manifest(self.book, result["id"])
        (self.book / doc["files"][-1]["target"]).write_bytes(b"corrupt")
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            self.restore(result["id"])
        self.assertEqual(inventory(self.book), before)

    def test_destination_collision_or_changed_retained_input_stops_complete_restore(self):
        result = self.apply()
        self.put("work/wav/A.wav", b"new edition waveform")
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            self.restore(result["id"])
        self.assertEqual(inventory(self.book), before)
        (self.book / "work/wav/A.wav").unlink()
        self.put("book.docx", b"new manuscript edited after cleanup")
        before = inventory(self.book)
        with self.assertRaisesRegex(m.MaintenanceError, "book.docx"):
            self.restore(result["id"])
        self.assertEqual(inventory(self.book), before)

    def test_nth_restore_failure_rolls_returned_files_back_to_quarantine(self):
        result = self.apply()
        original = ft.os.rename
        calls = 0
        def fail(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise PermissionError("restore locked")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=fail):
            with self.assertRaises(ft.TransactionError):
                self.restore(result["id"])
        doc = m._manifest(self.book, result["id"])
        self.assertEqual(doc["state"], "quarantined")
        for f in doc["files"]:
            self.assertEqual(m.digest(self.book / f["target"]), f["sha256"])
            self.assertFalse((self.book / f["source"]).exists())
        self.assertFalse((self.book / "work/.nox_prepare.lock").exists())
        self.restore(result["id"])

    def test_restore_crash_recovery_keeps_original_cleanup_available(self):
        result = self.apply()
        original = ft.os.rename
        calls = 0
        def crash(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt("restore process death")
            return original(source, target)
        with mock.patch.object(ft.os, "rename", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(result["id"])
        self.recover()
        self.assertEqual(m._manifest(self.book, result["id"])["state"], "quarantined")
        self.restore(result["id"])

    def test_foreign_preparation_marker_and_wrong_root_manifest_are_not_accepted(self):
        self.put_json("work/.nox_prepare.lock", {"pid": 9999, "token": "other preparation"})
        with self.assertRaises(m.MaintenanceError):
            self.recover()
        (self.book / "work/.nox_prepare.lock").unlink()
        result = self.apply()
        path = Path(result["manifest"])
        doc = json.loads(path.read_text())
        doc["root"] = str(self.book) + "_other"
        path.write_text(json.dumps(doc))
        before = inventory(self.book)
        with self.assertRaises(m.MaintenanceError):
            self.restore(result["id"])
        self.assertEqual(inventory(self.book), before)

    def test_powershell_wrappers_are_ascii_plan_by_default_and_have_no_destructive_commands(self):
        for name, mode in (("cleanup_k1a.ps1", "pipeline"), ("ochistka_redakcii_k1a.ps1", "edition")):
            source = (KIT / name).read_bytes().decode("ascii")
            self.assertIn("'--mode', '" + mode + "'", source)
            self.assertIn("if ($Apply)", source)
            self.assertIn("'--apply'", source)
            self.assertIn("'--no-backup'", source)
            self.assertIn("'--restore'", source)
            self.assertIn("'--recover'", source)
            for command in ("Remove-Item", "Stop-Process", "taskkill", "robocopy", "pip install"):
                self.assertNotIn(command.casefold(), source.casefold())


if __name__ == "__main__":
    unittest.main()
