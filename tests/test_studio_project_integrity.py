# -*- coding: utf-8 -*-
"""Project archive and cross-folder audio identity regression tests."""
import hashlib
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
