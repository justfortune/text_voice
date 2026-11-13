# -*- coding: utf-8 -*-
"""Cross-layer regression tests for the studio's fallback part planner."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "base"))
sys.path.insert(0, str(ROOT / "kit"))
import nox_audiobook as audiobook
import nox_studio as studio


class SharedPartDurationContractTests(unittest.TestCase):
    def test_source_duration_is_final_duration_times_export_speed(self):
        self.assertAlmostEqual(96.0, audiobook.part_source_seconds(2, 0.8))
        self.assertAlmostEqual(120.0, audiobook.part_source_seconds(2, 1.0))
        self.assertAlmostEqual(150.0, audiobook.part_source_seconds(2, 1.25))

    def test_invalid_part_duration_and_speed_are_rejected(self):
        for minutes, speed in ((0, 1), (-1, 1), (2, 0), (2, -1),
                               (float("inf"), 1), (2, float("nan"))):
            with self.subTest(minutes=minutes, speed=speed):
                with self.assertRaises(ValueError):
                    audiobook.part_source_seconds(minutes, speed)

    def test_studio_fallback_planner_uses_export_speed_not_voice_tempo(self):
        s = studio.Studio.__new__(studio.Studio)
        s.part_min = 2.0
        s.speed = 0.8
        s.rows = [
            {"pause_before_ms": 550, "kind": "text"},
            {"pause_before_ms": 550, "kind": "text"},
            {"pause_before_ms": 550, "kind": "text"},
        ]
        with tempfile.TemporaryDirectory() as td:
            s.wavs = []
            for name in ("a.wav", "b.wav", "c.wav"):
                wav = Path(td) / name
                wav.write_bytes(b"test")
                s.wavs.append(wav)
            with patch.object(studio, "n", audiobook), \
                 patch.object(audiobook, "load_durations", return_value={}), \
                 patch.object(audiobook, "duration_of", return_value=50.0):
                planned = s.own_plan(0)
        # At speed 0.8, 2 min final duration needs 96 s source.
        # Two 50-second phrases cross that threshold; TEMPO_G is irrelevant.
        self.assertEqual([(0, 1), (2, 2)], planned)


class PipelinePartIndexCompatibilityTests(unittest.TestCase):
    def _studio_with_index(self, root, entries):
        import json
        import types

        parts = Path(root) / "parts"
        parts.mkdir()
        index = Path(root) / "parts.json"
        index.write_text(json.dumps(entries), encoding="utf-8")
        s = studio.Studio.__new__(studio.Studio)
        s.rows = [{"id": i + 1} for i in range(4)]
        s.wavs = [parts / f"{name}.wav" for name in ("a", "b", "c", "d")]
        s._spans = {}
        s.id2i = {}
        s.ins_tail = lambda i: i
        return s, types.SimpleNamespace(PARTS=parts, PARTS_INDEX=index)

    def test_current_parts_index_without_legacy_sig_exposes_all_completed_parts(self):
        with tempfile.TemporaryDirectory() as td:
            s, pipeline = self._studio_with_index(td, [
                {"n": 1, "start_id": "1", "end_id": "2", "start": "a", "end": "b"},
                {"n": 2, "start_id": "3", "end_id": "4", "start": "c", "end": "d"},
            ])
            with patch.object(studio, "n", pipeline):
                self.assertEqual([(0, 1), (2, 3)], s.pipeline_parts(0))

    def test_plan_keeps_opened_part_and_discovers_following_pipeline_parts(self):
        import threading

        with tempfile.TemporaryDirectory() as td:
            s, pipeline = self._studio_with_index(td, [
                {"n": 1, "start_id": "1", "end_id": "2", "start": "a", "end": "b"},
                {"n": 2, "start_id": "3", "end_id": "4", "start": "c", "end": "d"},
            ])
            s.lock = threading.RLock()
            s._parts_cache = None
            s.store = {"parts": [{"last": "b.0", "last_i": 1, "last_p": 1}]}
            s.idx_of = {"a.0": 0, "b.0": 1, "c.0": 2, "d.0": 3}
            s.sidx = [0, 1, 2, 3]
            with patch.object(studio, "n", pipeline):
                planned = s.plan()
            self.assertEqual([
                {"n": 1, "a": 0, "b": 1, "opened": True},
                {"n": 2, "a": 2, "b": 3, "opened": False},
            ], planned)

    def test_stale_time_map_is_rejected_when_audio_boundary_identity_disagrees(self):
        with tempfile.TemporaryDirectory() as td:
            s, pipeline = self._studio_with_index(td, [
                {"n": 1, "start_id": "1", "end_id": "2", "start": "a", "end": "b"},
            ])
            # Simulate a surviving timeline map from an older book layout:
            # its row positions now point at b..c, not this part's a..b audio.
            with patch.object(studio, "n", pipeline), \
                 patch.object(s, "_part_span", return_value=(1, 2)):
                self.assertEqual([(0, 1)], s.pipeline_parts(0))

    def test_legacy_parts_index_with_sig_remains_supported(self):
        with tempfile.TemporaryDirectory() as td:
            s, pipeline = self._studio_with_index(td, [
                {"n": 1, "sig": "legacy", "start": "a", "end": "b"},
                {"n": 2, "sig": "legacy", "start": "c", "end": "d"},
            ])
            with patch.object(studio, "n", pipeline):
                self.assertEqual([(0, 1), (2, 3)], s.pipeline_parts(0))

    def test_incomplete_index_entry_stops_without_fabricating_later_parts(self):
        with tempfile.TemporaryDirectory() as td:
            s, pipeline = self._studio_with_index(td, [
                {"n": 1, "start_id": "1", "end_id": "2", "start": "a", "end": "b"},
                {"n": 2, "start": "c"},  # incomplete record
                {"n": 3, "start_id": "3", "end_id": "4", "start": "c", "end": "d"},
            ])
            with patch.object(studio, "n", pipeline):
                self.assertEqual([(0, 1)], s.pipeline_parts(0))



class CrossPartSynchronizationTests(unittest.TestCase):
    def test_book_clock_accumulates_time_maps_across_consecutive_parts(self):
        """A cue anchored in one part must retain its global position in the next part."""
        import types

        with tempfile.TemporaryDirectory() as td:
            parts = Path(td)
            (parts / "part_01.times.tsv").write_text(
                "00:00.0\t00:01.0\t1\tnarrator\ta\tfirst\t0.000\t1.000\n",
                encoding="utf-8",
            )
            (parts / "part_02.times.tsv").write_text(
                "00:00.0\t00:02.0\t2\tnarrator\tb\tsecond\t0.000\t2.000\n",
                encoding="utf-8",
            )
            s = studio.Studio.__new__(studio.Studio)
            s._clock = (0.0, {})
            s.id2i = {"1": 0, "2": 1}

            with patch.object(studio, "n", types.SimpleNamespace(PARTS=parts)):
                clock = s.book_clock()

        self.assertEqual((0.0, 1.0), clock[0])
        # The established inter-part gap is one second, not a reset to time zero.
        self.assertEqual((2.0, 4.0), clock[1])

    def test_cue_anchored_before_part_is_continued_when_end_anchor_is_inside(self):
        """A cue spanning a part boundary starts at local zero and carries its elapsed offset."""
        s = studio.Studio.__new__(studio.Studio)
        s.idx_of = {"before": 0, "inside": 1, "end": 2}
        s.cue_lead = lambda cue, i, a: 0.75
        cue = {"uid": "before", "e_uid": "end", "at": "s", "off": 0.0}
        current_segments = {"inside": (0.0, 1.0), "end": (1.0, 2.0)}

        result = s.cue_times(cue, current_segments, 1, 2, 3.0)

        self.assertIsNotNone(result)
        self.assertEqual(0.0, result[0])
        self.assertEqual(2.0, result[1])
        self.assertAlmostEqual(0.75, result[2])
        self.assertFalse(result[3])

    def test_cue_ending_after_part_is_marked_cut_not_dropped(self):
        """A cue that crosses the right edge remains present and is faded at the part boundary."""
        s = studio.Studio.__new__(studio.Studio)
        s.idx_of = {"start": 0, "inside": 1, "after": 2}
        s.cue_lead = lambda cue, i, a: 0.0
        cue = {"uid": "inside", "e_uid": "after", "at": "s", "off": 0.0, "e_at": "e", "e_off": 0.0}
        current_segments = {"inside": (0.5, 1.5)}

        result = s.cue_times(cue, current_segments, 0, 1, 2.0)

        self.assertIsNotNone(result)
        self.assertEqual(0.5, result[0])
        self.assertEqual(2.0, result[1])
        self.assertTrue(result[3])


if __name__ == "__main__":
    unittest.main()
