# -*- coding: utf-8 -*-
"""Regression tests for atomic part-boundary changes and export publication."""
import sys
import threading
import unittest
import types
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kit"))
import nox_studio as studio


def make_studio():
    s = studio.Studio.__new__(studio.Studio)
    s.lock = threading.RLock()
    s.auto_on = lambda: False
    s.rows = [
        {"kind": "text", "pause_before_ms": 0},
        {"kind": "text", "pause_before_ms": 0},
        {"kind": "head", "pause_before_ms": 0},
        {"kind": "text", "pause_before_ms": 0},
        {"kind": "text", "pause_before_ms": 0},
        {"kind": "head", "pause_before_ms": 0},
    ]
    s.uids = [f"u{i}" for i in range(len(s.rows))]
    s.idx_of = {uid: i for i, uid in enumerate(s.uids)}
    s.pidx = list(range(len(s.rows)))
    s.store = {"parts": [
        {"last": "u2", "last_i": 2, "last_p": 2, "locked": None,
         "eq": {"narrator": 1.0}, "export": {"ver": "old-current"}},
        {"last": "u5", "last_i": 5, "last_p": 5, "locked": None,
         "eq": {"narrator": 1.1}, "export": {"ver": "old-next"}},
    ]}
    s._parts_cache = (123.0, [{"n": 1, "a": 0, "b": 2, "opened": True},
                              {"n": 2, "a": 3, "b": 5, "opened": True}])
    s.plan = Mock(return_value=s._parts_cache[1])
    s.pipe_at = lambda i, _start: i
    s.ins_tail = lambda i: i
    s.save = Mock()
    return s


class BoundaryAtomicityTests(unittest.TestCase):
    def test_boundary_change_invalidates_both_role_tempo_caches_in_one_save(self):
        s = make_studio()
        with patch.object(studio, "n", types.SimpleNamespace(PAUSE={"para": 900})):
            s.set_boundary(1, "u1")

        current, following = s.store["parts"]
        self.assertEqual(("u1", 1, 1), (current["last"], current["last_i"], current["last_p"]))
        self.assertIsNone(current["eq"])
        self.assertIsNone(current["export"])
        self.assertIsNone(following["eq"])
        self.assertEqual({"ver": "old-next"}, following["export"])
        self.assertIsNone(s._parts_cache)
        s.save.assert_called_once()

    def test_boundary_change_during_whole_book_run_is_rejected_without_mutation(self):
        s = make_studio()
        s.auto_on = lambda: True
        before = [dict(x) for x in s.store["parts"]]
        with patch.object(studio, "n", types.SimpleNamespace(PAUSE={"para": 900})):
            with self.assertRaisesRegex(studio.Oops, "на паузе"):
                s.set_boundary(1, "u1")
        self.assertEqual(before, s.store["parts"])
        s.save.assert_not_called()

    def test_non_paragraph_boundary_is_rejected_without_mutation(self):
        s = make_studio()
        s.rows[2] = {"kind": "text", "pause_before_ms": 0}
        before = [dict(x) for x in s.store["parts"]]
        with patch.object(studio, "n", types.SimpleNamespace(PAUSE={"para": 900})):
            with self.assertRaisesRegex(studio.Oops, "конце абзаца"):
                s.set_boundary(1, "u1")
        self.assertEqual(before, s.store["parts"])
        s.save.assert_not_called()

    def test_save_failure_rolls_back_both_part_records_and_cache(self):
        s = make_studio()
        original = [dict(x) for x in s.store["parts"]]
        old_cache = s._parts_cache
        s.save.side_effect = OSError("simulated atomic save failure")
        with patch.object(studio, "n", types.SimpleNamespace(PAUSE={"para": 900})):
            with self.assertRaisesRegex(OSError, "simulated"):
                s.set_boundary(1, "u1")
        self.assertEqual(original, s.store["parts"])
        self.assertEqual(old_cache, s._parts_cache)

    def test_same_end_is_not_saved_as_a_boundary_change(self):
        s = make_studio()
        with patch.object(studio, "n", types.SimpleNamespace(PAUSE={"para": 900})):
            with self.assertRaisesRegex(studio.Oops, "уже заканчивается"):
                s.set_boundary(1, "u2")
        s.save.assert_not_called()

    def test_export_is_rejected_when_part_start_moved_but_end_uid_did_not(self):
        s = studio.Studio.__new__(studio.Studio)
        s.plan = lambda: [{"n": 1, "a": 2, "b": 5, "opened": True}]
        with self.assertRaisesRegex(studio.Oops, "границы части 1 изменились"):
            s._assert_part_range_current(1, 0, 5)

    def test_export_range_guard_accepts_unchanged_part(self):
        s = studio.Studio.__new__(studio.Studio)
        s.plan = lambda: [{"n": 1, "a": 0, "b": 5, "opened": True}]
        self.assertIsNone(s._assert_part_range_current(1, 0, 5))


if __name__ == "__main__":
    unittest.main()
