# -*- coding: utf-8 -*-
"""Pure state-integrity tests for the studio's existing phrase-chain model."""
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
