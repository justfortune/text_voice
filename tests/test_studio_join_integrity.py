# -*- coding: utf-8 -*-
"""Pure state-integrity tests for the studio's existing phrase-chain model."""
import ctypes
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kit"))
import nox_studio as studio


def make_studio(uids, segs, pidx=None):
    s = studio.Studio.__new__(studio.Studio)
    s.uids = list(uids)
    s.idx_of = {uid: i for i, uid in enumerate(uids)}
    s.pidx = list(pidx if pidx is not None else [1] * len(uids))
    s.store = {"segs": segs}
    s._abs = {}
    return s


class PhraseChainIntegrityTests(unittest.TestCase):
    def test_valid_multi_phrase_chain_is_reconstructed_in_book_order(self):
        s = make_studio(
            ["a", "b", "c", "d"],
            {
                "a": {"join": ["b", "c"], "jwas": [{}, {}]},
                "b": {"into": "a"},
                "c": {"into": "a"},
            },
        )
        self.assertEqual(0, s.joins_refresh())
        self.assertEqual({1: 0, 2: 0}, s._abs)
        self.assertEqual([0, 1, 2], s.chain(0))
        self.assertEqual([1], s.chain(1))
        self.assertEqual([3], s.chain(3))

    def test_non_adjacent_join_is_removed_and_head_state_restored(self):
        s = make_studio(
            ["a", "inserted", "b"],
            {
                "a": {"join": ["b"], "jwas": [{"tech": "original wording"}], "tech": "joined wording"},
                "b": {"into": "a"},
            },
        )
        self.assertEqual(1, s.joins_refresh())
        self.assertEqual({}, s._abs)
        self.assertNotIn("join", s.store["segs"]["a"])
        self.assertNotIn("jwas", s.store["segs"]["a"])
        self.assertEqual("original wording", s.store["segs"]["a"]["tech"])
        self.assertNotIn("into", s.store["segs"]["b"])

    def test_join_crossing_unassigned_part_boundary_is_removed(self):
        s = make_studio(
            ["a", "b"],
            {"a": {"join": ["b"], "jwas": [{}]}, "b": {"into": "a"}},
            pidx=[1, None],
        )
        self.assertEqual(1, s.joins_refresh())
        self.assertFalse(s._abs)
        self.assertNotIn("join", s.store["segs"]["a"])
        self.assertNotIn("into", s.store["segs"]["b"])

    def test_orphan_into_and_head_lock_marker_are_cleaned(self):
        s = make_studio(
            ["a", "b"],
            {"a": {"locked": {"head": True}}, "b": {"into": "a", "locked": {"head": True}}},
        )
        self.assertEqual(0, s.joins_refresh())
        self.assertNotIn("into", s.store["segs"]["b"])
        self.assertNotIn("locked", s.store["segs"]["b"])

    def test_empty_chain_does_not_change_regular_phrase(self):
        s = make_studio(["a", "b"], {"a": {}, "b": {}})
        self.assertEqual(0, s.joins_refresh())
        self.assertEqual([0], s.chain(0))
        self.assertEqual([1], s.chain(1))



class StudioPidSafetyTests(unittest.TestCase):
    def _windows_api(self, open_handle=1234, exit_code=259, exit_ok=1):
        class ApiCall:
            def __init__(self, fn):
                self.fn = fn
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.fn(*args)

        closed = []
        def get_exit_code(handle, pointer):
            if exit_ok:
                pointer._obj.value = exit_code
            return exit_ok

        kernel32 = type("Kernel32", (), {})()
        kernel32.OpenProcess = ApiCall(lambda *args: open_handle)
        kernel32.GetExitCodeProcess = ApiCall(get_exit_code)
        kernel32.CloseHandle = ApiCall(lambda handle: closed.append(handle) or 1)
        return kernel32, closed

    def test_windows_live_and_exited_processes_are_distinguished(self):
        for exit_code, expected in ((259, True), (0, False)):
            with self.subTest(exit_code=exit_code):
                kernel32, closed = self._windows_api(exit_code=exit_code)
                with patch.object(studio.os, "name", "nt"), \
                     patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
                    self.assertIs(expected, studio.pid_alive(123))
                self.assertEqual([1234], closed)

    def test_windows_access_denied_is_not_mistaken_for_dead_process(self):
        kernel32, _ = self._windows_api(open_handle=0)
        with patch.object(studio.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True), \
             patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertTrue(studio.pid_alive(123))

    def test_windows_missing_pid_is_confirmed_only_by_invalid_parameter(self):
        kernel32, _ = self._windows_api(open_handle=0)
        with patch.object(studio.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True), \
             patch.object(ctypes, "get_last_error", return_value=87, create=True):
            self.assertFalse(studio.pid_alive(123))

    def test_windows_exit_code_query_failure_is_conservative(self):
        kernel32, closed = self._windows_api(exit_ok=0)
        with patch.object(studio.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
            self.assertTrue(studio.pid_alive(123))
        self.assertEqual([1234], closed)

    def test_windows_api_load_failure_is_conservative(self):
        with patch.object(studio.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", side_effect=OSError("kernel32 unavailable"), create=True):
            self.assertTrue(studio.pid_alive(123))

    def test_posix_existing_process_is_alive(self):
        with patch.object(studio.os, "name", "posix"), \
             patch.object(studio.os, "kill") as kill:
            self.assertTrue(studio.pid_alive(123))
            kill.assert_called_once_with(123, 0)

    def test_posix_missing_process_is_dead(self):
        with patch.object(studio.os, "name", "posix"), \
             patch.object(studio.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(studio.pid_alive(123))

    def test_posix_permission_error_is_conservative(self):
        with patch.object(studio.os, "name", "posix"), \
             patch.object(studio.os, "kill", side_effect=PermissionError):
            self.assertTrue(studio.pid_alive(123))

    def test_non_positive_pid_is_not_alive(self):
        self.assertFalse(studio.pid_alive(0))
        self.assertFalse(studio.pid_alive(-1))


class JoinSaveRollbackTests(unittest.TestCase):
    def _studio(self):
        import threading
        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.uids = ["a", "b"]
        s.idx_of = {"a": 0, "b": 1}
        s.rows = [{"id": 1}, {"id": 2}]
        s._abs = {}
        s.jobs = {}
        s.undo = [("older", "snapshot", "older change")]
        s.store = {"segs": {
            "a": {"sid": "sa", "join": ["b"], "jwas": [{"tech": "before"}], "tech": "joined"},
            "b": {"sid": "sb", "into": "a"},
        }}
        s.joins_refresh = lambda: 0
        s._tidy = lambda uid: None
        s.save = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated disk failure"))
        return s

    def test_split_save_failure_restores_phrase_state_and_undo_history(self):
        s = self._studio()
        before = studio.jd(s.store["segs"])
        undo_before = list(s.undo)
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.join_split("a")
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(undo_before, s.undo)

    def test_join_undo_save_failure_restores_state_and_keeps_undo_entry(self):
        s = self._studio()
        s.store["segs"] = {"a": {"sid": "sa"}, "b": {"sid": "sb"}}
        undo_snapshot = studio.jd({
            "a": "a", "ea": {"sid": "sa", "join": ["b"], "jwas": [{"tech": "before"}]},
            "b": "b", "eb": {"sid": "sb", "into": "a"},
        })
        s.undo = [("#join", undo_snapshot, "split")]
        before = studio.jd(s.store["segs"])
        undo_before = list(s.undo)
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.undo_last()
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(undo_before, s.undo)


class GlueArtifactRollbackTests(unittest.TestCase):
    def test_metadata_failure_removes_only_new_glue_artifacts(self):
        import tempfile
        import types
        import numpy as np

        with tempfile.TemporaryDirectory() as root:
            s = studio.Studio.__new__(studio.Studio)
            s.takes_dir = Path(root)
            s._takes = {}
            s.read_audio = lambda path: (np.ones(2400, dtype="float32"), 24000)
            s.row_text = lambda index: "joined phrase"
            e = {"sid": "sa"}
            fake_soundfile = types.SimpleNamespace(
                write=lambda path, audio, rate, subtype: Path(path).write_bytes(b"wav")
            )
            with patch.dict(sys.modules, {"soundfile": fake_soundfile}), \
                 patch.object(studio.n, "NORM_ON", False), \
                 patch.object(studio, "write_atomic", side_effect=OSError("metadata write failed")):
                with self.assertRaisesRegex(OSError, "metadata write failed"):
                    s._glue(0, e, {"path": "first"}, {"path": "second"}, "voice", "joined phrase")

            take_dir = Path(root) / "sa"
            self.assertFalse(take_dir.exists() and list(take_dir.iterdir()))
            self.assertEqual(1, e["tseq"])




if __name__ == "__main__":
    unittest.main()
