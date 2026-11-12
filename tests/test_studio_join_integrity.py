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
                with patch.object(studio.os, "name", "nt"), \\
                     patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
                    self.assertIs(expected, studio.pid_alive(123))
                self.assertEqual([1234], closed)

    def test_windows_access_denied_is_not_mistaken_for_dead_process(self):
        kernel32, _ = self._windows_api(open_handle=0)
        with patch.object(studio.os, "name", "nt"), \\
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True), \\
             patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertTrue(studio.pid_alive(123))

    def test_windows_missing_pid_is_confirmed_only_by_invalid_parameter(self):
        kernel32, _ = self._windows_api(open_handle=0)
        with patch.object(studio.os, "name", "nt"), \\
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True), \\
             patch.object(ctypes, "get_last_error", return_value=87, create=True):
            self.assertFalse(studio.pid_alive(123))

    def test_windows_exit_code_query_failure_is_conservative(self):
        kernel32, closed = self._windows_api(exit_ok=0)
        with patch.object(studio.os, "name", "nt"), \\
             patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
            self.assertTrue(studio.pid_alive(123))
        self.assertEqual([1234], closed)

    def test_non_positive_pid_is_not_alive(self):
        self.assertFalse(studio.pid_alive(0))
        self.assertFalse(studio.pid_alive(-1))


if __name__ == "__main__":
    unittest.main()
