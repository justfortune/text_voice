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


if __name__ == "__main__":
    unittest.main()
