# -*- coding: utf-8 -*-
"""Project archive and cross-folder audio identity regression tests."""
import hashlib
import json
import threading
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kit"))
import nox_studio as studio


class ProjectAudioIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = studio.Studio.__new__(studio.Studio)
        self.s.dir = self.root / "studio"
        self.s.takes_dir = self.s.dir / "takes"
        self.s.sounds_dir = self.s.dir / "sounds"
        self.s.imports_dir = self.s.dir / "imports"

    def _active_take(self, sid="s000001", tid="t001", data=b"local-audio"):
        path = self.s.takes_dir / sid / f"{sid}_{tid}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _trash_take(self, sid="s000001", tid="t001", data=b"local-audio"):
        path = self.s.dir / "trash" / sid / f"{sid}_{tid}__deleted.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _entry(self, sid="s000001", tid="t001"):
        arc = f"takes/{sid}/{sid}_{tid}.wav"
        return arc

    def test_foreign_no_audio_project_does_not_reuse_same_size_active_take(self):
        local = self._active_take(data=b"same-size")
        arc = self._entry()
        pf = {"t001": (arc, local.stat().st_size, None)}

        self.assertTrue(self.s._proj_sid_conflict("s000001", True, pf, set(), {}))

    def test_foreign_no_audio_project_does_not_restore_unverified_trashed_take(self):
        trashed = self._trash_take(data=b"same-size")
        arc = self._entry()
        pf = {"t001": (arc, trashed.stat().st_size, None)}

        self.assertTrue(self.s._proj_sid_conflict("s000001", True, pf, set(), {}))

    def test_same_home_project_can_keep_its_local_unembedded_take(self):
        local = self._active_take(data=b"same-size")
        arc = self._entry()
        pf = {"t001": (arc, local.stat().st_size, None)}

        self.assertFalse(self.s._proj_sid_conflict("s000001", False, pf, set(), {}))

    def test_embedded_project_audio_is_compared_by_hash_even_at_same_size(self):
        expected = b"voice-a"
        local = self._active_take(data=b"voice-b")
        self.assertEqual(len(expected), local.stat().st_size)
        arc = self._entry()
        pf = {"t001": (arc, len(expected), hashlib.sha1(expected).hexdigest())}

        self.assertTrue(self.s._proj_sid_conflict("s000001", True, pf, {arc}, {}))

        local.write_bytes(expected)
        self.assertFalse(self.s._proj_sid_conflict("s000001", True, pf, {arc}, {}))

    def test_project_preflight_marks_foreign_unembedded_local_take_unverified(self):
        local = self._active_take(data=b"same-size")
        arc = self._entry()
        self.s.take_path = lambda sid, tid: local if (sid, tid) == ("s000001", "t001") else self.root / "absent.wav"

        result = self.s._proj_audio_check({}, {arc: local.stat().st_size}, foreign=True)

        self.assertEqual(1, result["unverified"])
        self.assertEqual(1, result["have"])



    def test_same_size_import_asset_with_different_hash_is_reported(self):
        local = self.root / "studio" / "imports" / "s000001_t001_original.wav"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"local01")
        arc = "imports/s000001_t001_original.wav"
        expected = b"sourceX"
        self.assertEqual(len(expected), local.stat().st_size)

        conflicts = self.s._proj_asset_conflicts({
            arc: [len(expected), hashlib.sha1(expected).hexdigest()]
        })

        self.assertEqual([arc], conflicts)

    def test_same_size_sound_with_matching_hash_is_not_a_conflict(self):
        local = self.root / "studio" / "sounds" / "door_12345678.wav"
        local.parent.mkdir(parents=True, exist_ok=True)
        payload = b"same-sound"
        local.write_bytes(payload)
        arc = "sounds/door_12345678.wav"

        conflicts = self.s._proj_asset_conflicts({
            arc: [len(payload), hashlib.sha1(payload).hexdigest()]
        })

        self.assertEqual([], conflicts)

    def test_project_load_aborts_asset_collision_before_creating_snapshot(self):
        import threading
        local = self.root / "studio" / "imports" / "same.wav"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"local-a")
        project_audio = b"other-a"
        self.assertEqual(len(project_audio), local.stat().st_size)
        archive = Mock()
        archive.close = Mock()

        s = studio.Studio.__new__(studio.Studio)
        s.dir = self.root / "studio"
        s.sounds_dir = s.dir / "sounds"
        s.lock = threading.RLock()
        s._proj_read = lambda _path: (
            archive,
            {"title": "Test", "home": "different"},
            {"segs": {}, "parts": []},
            {"imports/same.wav": [len(project_audio), hashlib.sha1(project_audio).hexdigest()]},
            {},
        )
        s._proj_compat = lambda _m, _st: {
            "same": True, "other": False, "found": 0, "lost": 0, "total": 0
        }
        s.project_save = Mock()

        with self.assertRaisesRegex(studio.Oops, "совпадают с местными по имени и размеру"):
            s._project_load(self.root / "external.noxproj")

        s.project_save.assert_not_called()
        archive.close.assert_called_once_with()
        self.assertFalse((s.dir / "projects" / "auto").exists())
        self.assertFalse((s.dir / "edits.json").exists())


class ProjectArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = studio.Studio.__new__(studio.Studio)

    def test_project_check_does_not_create_home_id(self):
        archive = self.root / "external.noxproj"
        state = {"segs": {}, "parts": []}
        edits = json.dumps(state).encode("utf-8")
        manifest = {
            "format": "noxproj",
            "ver": studio.Studio.PROJ_FMT,
            "edits_sha": hashlib.sha1(edits).hexdigest(),
            "home": "another-folder-id",
            "title": "Test book",
        }
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("edits.json", edits)
            zf.writestr("files.json", json.dumps({"files": {}, "need": {}}))

        s = studio.Studio.__new__(studio.Studio)
        s.dir = self.root / "current-studio"
        s.lock = threading.RLock()
        s.store = {"segs": {}, "parts": []}
        s.roles = {"roles": []}
        s._proj_path = lambda _name: archive
        s._proj_compat = lambda _m, _st: {
            "same": True, "other": False, "found": 0, "lost": 0, "total": 0
        }
        s._proj_info = lambda _path: {}
        s._proj_audio_check = lambda _files, _need, foreign=False: {
            "have": 0, "add": 0, "differ": 0, "trash": 0, "missing": 0, "unverified": 0
        }
        s._proj_busy_why = lambda: ""

        result = s.project_check("external.noxproj")

        self.assertTrue(result["ok"])
        self.assertFalse((s.dir / "home.id").exists())
        self.assertTrue(any("другой папке" in warning for warning in result["warn"]))

    def test_reader_closes_zip_when_manifest_cannot_be_read(self):
        fake_zip = Mock()
        fake_zip.read.side_effect = KeyError("manifest.json")

        with patch("zipfile.ZipFile", return_value=fake_zip):
            with self.assertRaisesRegex(studio.Oops, "не читается как файл проекта"):
                self.s._proj_read(self.root / "broken.noxproj")

        fake_zip.close.assert_called_once_with()

    def test_bad_audio_checksum_does_not_replace_existing_take(self):
        archive = self.root / "project.zip"
        arc = "takes/s000001/s000001_t001.wav"
        payload = b"new-audio"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(arc, payload)

        target = self.root / "takes" / "s000001" / "s000001_t001.wav"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old-audio")

        with zipfile.ZipFile(archive) as zf, patch.object(studio, "log"):
            result = self.s._zip_extract(zf, arc, target, "0" * 40)

        self.assertFalse(result)
        self.assertEqual(b"old-audio", target.read_bytes())
        self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_valid_audio_checksum_is_published_without_temp_file(self):
        archive = self.root / "project.zip"
        arc = "takes/s000001/s000001_t001.wav"
        payload = b"verified-audio"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(arc, payload)

        target = self.root / "takes" / "s000001" / "s000001_t001.wav"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old-audio")

        with zipfile.ZipFile(archive) as zf:
            result = self.s._zip_extract(zf, arc, target, hashlib.sha1(payload).hexdigest())

        self.assertTrue(result)
        self.assertEqual(payload, target.read_bytes())
        self.assertFalse(target.with_name(target.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
