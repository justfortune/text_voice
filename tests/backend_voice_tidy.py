"""Actual voice tidy entry point: paired quarantine and crash recovery."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from protection_regressions import KIT, rows, state, fixture, put, generated_module
import nox_freeze as nf
import nox_voice_tidy as tidy
import nox_voice_safety as safety


class VoiceTidy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "book"
        self.bank = Path(self.tmp.name) / "bank"
        self.rr = rows("Первая фраза.")
        fixture(self.root, self.rr)
        self.n = generated_module(self.root)
        self.roles = self.n.load_roles()
        self.pair = self.n.voice_files("narrator", self.n.voice_stems(self.roles))
        for p in self.pair:
            put(self.root, str(p.relative_to(self.root)), ("current " + p.name).encode())
        self.orphans = [put(self.root, "work/voices/orphan" + ext, ("orphan" + ext).encode()) for ext in (".wav", ".txt", ".json")]
        self.stray = [put(self.bank, "stray" + ext, ("stray" + ext).encode()) for ext in (".wav", ".txt")]
        self.bankjson = put(self.bank, "bank.json", {"ver": 1, "voices": {}})
        spec = importlib.util.spec_from_file_location("tested_nox_voices_tidy", KIT / "nox_voices.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def tearDown(self):
        self.tmp.cleanup()

    def run_actual(self, apply=False):
        output = io.StringIO()
        with patch.object(self.script, "load_n", return_value=self.n), patch.object(self.script, "BANK", self.bank), contextlib.redirect_stdout(output):
            self.assertEqual(self.script.cmd_tidy(types.SimpleNamespace(root=str(self.root), apply=apply, json=True)), 0)
        return json.loads(output.getvalue())

    def assets(self):
        return {p: p.read_bytes() for p in [*self.pair, *self.orphans, *self.stray, self.n.ROLES_PATH, self.bankjson] if p.exists()}

    def assert_assets(self, before):
        self.assertEqual(self.assets(), before)
        self.assertFalse((self.root / "work/.nox_roles.lock").exists())

    def test_actual_preview_is_readonly_and_apply_archives_complete_bundles(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.tmp.name).rglob("*") if p.is_file()}
        preview = self.run_actual()
        self.assertFalse(preview["applied"])
        self.assertEqual(set(preview["book"]["orphans"]), {p.name for p in self.orphans})
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.tmp.name).rglob("*") if p.is_file()}, before)
        result = self.run_actual(True)
        journal = json.loads(Path(result["journal"]).read_text())
        self.assertEqual(journal["state"], "committed")
        self.assertEqual(journal["plan_sha256"], tidy._seal(journal))
        for item in journal["moves"]:
            home = self.root if item["scope"] == "book" else self.bank
            self.assertFalse((home / item["source"]).exists())
            self.assertEqual(hashlib.sha256((home / item["target"]).read_bytes()).hexdigest(), item["sha256"])
        for p in self.pair:
            self.assertEqual(p.read_bytes(), before[p][0])

    def test_archive_collision_and_repeated_names_never_overwrite(self):
        token = "a" * 32
        occupied = put(self.root, "work/voices_old/bez_roli/" + token + "/orphan.wav", b"earlier exact directory")
        before = self.assets()
        with patch.object(tidy.uuid, "uuid4", return_value=types.SimpleNamespace(hex=token)), self.assertRaises(FileExistsError):
            self.run_actual(True)
        self.assertEqual(occupied.read_bytes(), b"earlier exact directory")
        self.assert_assets(before)
        first = self.run_actual(True)
        for p in self.orphans:
            p.write_bytes(b"new version " + p.suffix.encode())
        second = self.run_actual(True)
        self.assertNotEqual(first["book"]["to"], second["book"]["to"])
        self.assertEqual((Path(first["book"]["to"]) / "orphan.wav").read_bytes(), before[self.orphans[0]])
        self.assertEqual((Path(second["book"]["to"]) / "orphan.wav").read_bytes(), b"new version .wav")

    def test_second_media_move_failure_rolls_back_every_pair(self):
        before = self.assets()
        original, calls = tidy._move, []
        def fail(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:
                raise OSError("injected second move failure")
            original(src, dst)
        with patch.object(tidy, "_move", side_effect=fail), self.assertRaisesRegex(OSError, "injected"):
            self.run_actual(True)
        self.assert_assets(before)
        journal = next((self.root / "work/voice_history/tidy").glob("*/manifest.json"))
        self.assertEqual(json.loads(journal.read_text())["state"], "rolled_back")

    def crash_after_first_move(self):
        original, calls = tidy._move, []
        def crash(src, dst):
            original(src, dst)
            calls.append((src, dst))
            raise SystemExit("simulated hard stop")
        with patch.object(tidy, "_move", side_effect=crash), self.assertRaises(SystemExit):
            self.run_actual(True)
        return calls[0]

    def test_dead_process_recovery_via_existing_roles_entry_point(self):
        before = self.assets()
        self.crash_after_first_move()
        with self.assertRaisesRegex(nf.ProtectionError, "процесс очистки"):
            safety.recover_roles(self.root)
        with patch.object(tidy, "_pid_alive", return_value=False):
            self.assertIn("восстановлена", safety.recover_roles(self.root))
        self.assert_assets(before)

    def test_recovery_checks_all_hashes_before_restoring_any_file(self):
        before = self.assets()
        src, dst = self.crash_after_first_move()
        dst.write_bytes(b"unknown independent modification")
        with patch.object(tidy, "_pid_alive", return_value=False), self.assertRaisesRegex(nf.ProtectionError, "вне операции"):
            tidy.recover_tidy(self.root)
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_bytes(), b"unknown independent modification")
        self.assertEqual(self.n.ROLES_PATH.read_bytes(), before[self.n.ROLES_PATH])
        dst.write_bytes(before[src])
        with patch.object(tidy, "_pid_alive", return_value=False):
            tidy.recover_tidy(self.root)
        self.assert_assets(before)

    def test_verified_history_and_locked_bank_references_are_kept(self):
        fx, ed = state(self.rr)
        put(self.root, "studio/fixed_parts.json", fx)
        put(self.root, "studio/edits.json", ed)
        self.roles["roles"]["narrator"]["voice"] = {"bank": "protected_missing"}
        put(self.root, "work/roles.json", self.roles)
        old_roles = {"roles": {"Retired": {"voice": {"bank": "historic_missing"}}}, "aliases": {}}
        old_path = put(self.root, "work/freeze_backups/old/work/roles.json", old_roles)
        put(self.root, "work/freeze_backups/old/manifest.json", {"state": "committed", "files": {"work/roles.json": tidy._hash(old_path)}})
        old_pair = self.n.voice_files("Retired", self.n.voice_stems(old_roles))
        for p in old_pair:
            put(self.root, p.relative_to(self.root), b"historical referenced sample")
        put(self.bank, "bank.json", {"ver": 1, "voices": {key: {"kind": "custom"} for key in ("protected_missing", "historic_missing")}})
        result = self.run_actual(True)
        self.assertEqual(result["bank"]["dead"], [])
        self.assertEqual(result["roles"]["unbound"], [])
        for p in old_pair:
            self.assertEqual(p.read_bytes(), b"historical referenced sample")
        self.assertEqual(self.n.load_roles()["roles"]["narrator"]["voice"]["bank"], "protected_missing")

    def test_corrupt_bank_or_history_refuses_without_moving_samples(self):
        self.bankjson.write_bytes(b'{"voices":')
        before = self.assets()
        with self.assertRaisesRegex(nf.ProtectionError, "повреждён"):
            self.run_actual(True)
        self.assert_assets(before)
        put(self.bank, "bank.json", {"voices": {}})
        put(self.root, "work/voice_history/roles_" + "0" * 64 + ".json", self.roles)
        before = self.assets()
        with self.assertRaisesRegex(nf.ProtectionError, "не подтверждён"):
            self.run_actual(True)
        self.assert_assets(before)

    def test_part_selected_metadata_and_override_roles_keep_bank_links(self):
        fixed, edits = state(self.rr)
        uid = nf.row_uids(self.rr)[0]
        edits["segs"][uid].pop("locked")
        edits["segs"][uid].update(take="t003", role="Selected override")
        put(self.root, "studio/fixed_parts.json", fixed)
        put(self.root, "studio/edits.json", edits)
        sid = edits["segs"][uid]["sid"]
        put(self.root, f"studio/takes/{sid}/{sid}_t003.wav", b"RIFF" + b"sample" * 20)
        put(self.root, f"studio/takes/{sid}/{sid}_t003.json", {"tid": "t003", "role": "Recorded role"})
        for role, bank_id in (("Selected override", "override_missing"), ("Recorded role", "recorded_missing")):
            self.roles["roles"][role] = {"voice": {"bank": bank_id}}
        put(self.root, "work/roles.json", self.roles)
        put(self.bank, "bank.json", {"voices": {v: {"kind": "custom"} for v in ("override_missing", "recorded_missing")}})
        report = self.run_actual(True)
        self.assertEqual(report["bank"]["dead"], [])
        self.assertEqual(report["roles"]["unbound"], [])
        self.assertIn("Selected override", report["protection"]["roles"])
        self.assertIn("Recorded role", report["protection"]["roles"])
        self.assertNotIn("locked", json.loads((self.root / "studio/edits.json").read_text())["segs"][uid])

    def test_metadata_failure_restores_documents_and_media_together(self):
        self.roles["roles"]["narrator"].update(voice={"bank": "missing"}, voice_prev={"file": "gone"})
        put(self.root, "work/roles.json", self.roles)
        put(self.bank, "bank.json", {"voices": {"missing": {"kind": "custom"}}})
        before = self.assets()
        original, failed = tidy._atomic_bytes, []
        def fail(path, data):
            if Path(path) == self.bankjson and not failed:
                failed.append(True)
                raise PermissionError("injected metadata refusal")
            original(path, data)
        with patch.object(tidy, "_atomic_bytes", side_effect=fail), self.assertRaisesRegex(PermissionError, "injected"):
            self.run_actual(True)
        self.assert_assets(before)
        result = self.run_actual(True)
        self.assertEqual(result["roles"], {"unbound": ["narrator"], "prev_gone": ["narrator"]})
        self.assertNotIn("voice", self.n.load_roles()["roles"]["narrator"])
        self.assertEqual(json.loads(self.bankjson.read_text())["voices"], {})


if __name__ == "__main__":
    unittest.main()
