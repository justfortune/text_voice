# -*- coding: utf-8 -*-
"""Regression tests for data-integrity fixes in nox_audiobook.py."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "base"))
import nox_audiobook as n


class ChunkTextTests(unittest.TestCase):
    def test_long_sentence_without_punctuation_is_bounded(self):
        words = ["слово"] * 110
        result = n.chunk_text(" ".join(words), 60)
        self.assertTrue(result)
        self.assertTrue(all(0 < len(part) <= 60 for part in result))
        self.assertEqual(" ".join(result).split(), words)

    def test_oversized_single_token_is_bounded(self):
        token = "я" * 137
        result = n.chunk_text(token, 30)
        self.assertEqual([30, 30, 30, 30, 17], [len(x) for x in result])
        self.assertEqual(token, "".join(result))

    def test_empty_text(self):
        self.assertEqual([], n.chunk_text("   ", 20))


class PartDurationPlanningTests(unittest.TestCase):
    def _plan(self, speed, frozen=None, duration=60.0):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": i + 1, "pause_before_ms": pause, "kind": "text"}
                for i, pause in enumerate((0, 1000, 1000, 1000))
            ]
            wavs = {str(row["id"]): root / (str(row["id"]) + ".wav") for row in rows}
            for wav in wavs.values():
                wav.write_bytes(b"wav")
            index = root / "parts.json"
            if frozen is not None:
                index.write_text(json.dumps(frozen), encoding="utf-8")
            durations = root / "durations.json"
            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", durations), \
                 patch.object(n, "PAUSE", {"para": 1000}), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \
                 patch.object(n, "duration_of", return_value=duration):
                return n.plan_parts(rows, {}, 2.0, speed=speed)[0]

    def test_faster_export_plans_more_source_audio_for_same_final_duration(self):
        normal = self._plan(1.0)
        faster = self._plan(1.25)
        self.assertEqual(1, normal[0][1])
        self.assertEqual(2, faster[0][1])

    def test_slower_export_uses_shorter_source_threshold(self):
        slower = self._plan(0.8, duration=50.0)
        normal = self._plan(1.0, duration=50.0)
        self.assertEqual(1, slower[0][1])
        self.assertEqual(2, normal[0][1])

    def test_existing_frozen_boundaries_do_not_move_when_speed_changes(self):
        frozen = [{
            "n": 1, "start_id": "1", "end_id": "2",
            "start": "1", "end": "2",
        }]
        self.assertEqual(1, self._plan(1.5, frozen=frozen)[0][1])

    def test_reused_sequential_ids_cannot_rebind_parts_to_a_different_edition(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": i, "text": f"new-{i}", "pause_before_ms": 1000, "kind": "text"}
                for i in range(1, 5)
            ]
            wavs = {str(row["id"]): root / f"new-{row['id']}.wav" for row in rows}
            for wav in wavs.values():
                wav.write_bytes(b"wav")
            index = root / "parts.json"
            index.write_text(json.dumps([{
                "n": 1, "start_id": "1", "end_id": "2",
                "start": "old-1", "end": "old-2",
            }]), encoding="utf-8")
            with patch.object(n, "PARTS_INDEX", index), \\
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \\
                 patch.object(n, "PAUSE", {"para": 1000}), \\
                 patch.object(n, "load_durations", return_value={}), \\
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \\
                 patch.object(n, "duration_of", return_value=40.0):
                with self.assertRaisesRegex(ValueError, "другой редакции"):
                    n.plan_parts(rows, {}, 2.0)

    def test_frozen_part_signature_detects_middle_segment_edits(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": i, "text": f"line-{i}", "pause_before_ms": 1000, "kind": "text"}
                for i in range(1, 4)
            ]
            keys = [f"voice-{i}" for i in range(1, 4)]
            wavs = {str(row["id"]): root / f"{keys[row['id'] - 1]}.wav" for row in rows}
            for wav in wavs.values():
                wav.write_bytes(b"wav")
            index = root / "parts.json"
            index.write_text(json.dumps([{
                "n": 1, "start_id": "1", "end_id": "3",
                "start": keys[0], "end": keys[-1], "sig": "old-signature",
            }]), encoding="utf-8")
            with patch.object(n, "PARTS_INDEX", index), \\
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \\
                 patch.object(n, "PAUSE", {"para": 1000}), \\
                 patch.object(n, "load_durations", return_value={}), \\
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \\
                 patch.object(n, "duration_of", return_value=40.0):
                with self.assertRaisesRegex(ValueError, "содержимое части изменилось"):
                    n.plan_parts(rows, {}, 2.0)

    def test_invalid_speed_is_rejected_before_planning(self):
        import math
        for speed in (0, -1, float("inf"), float("nan")):
            with self.subTest(speed=speed):
                with self.assertRaisesRegex(ValueError, "скорость сборки"):
                    n.plan_parts([], {}, 2.0, speed=speed)


class DurationCacheTests(unittest.TestCase):
    def test_cache_invalidates_when_wav_changes(self):
        fake_sf = types.SimpleNamespace(
            info=lambda path: types.SimpleNamespace(
                frames=Path(path).stat().st_size, samplerate=100
            )
        )
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "sample.wav"
            cache = {}
            with patch.dict(sys.modules, {"soundfile": fake_sf}):
                wav.write_bytes(b"a")
                first = n.duration_of(wav, cache)
                wav.write_bytes(b"abcdef")
                second = n.duration_of(wav, cache)
            self.assertAlmostEqual(0.01, first)
            self.assertAlmostEqual(0.06, second)


class PartIndexTests(unittest.TestCase):
    def test_ambiguous_legacy_hash_boundaries_fail_closed(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "parts.json"
            index.write_text(json.dumps([{"n": 1, "start": "same", "end": "same"}]), encoding="utf-8")
            rows = [
                {"id": 1, "speaker": "narrator", "text": "повтор", "pause_before_ms": 0, "kind": "text"},
                {"id": 2, "speaker": "narrator", "text": "повтор", "pause_before_ms": 0, "kind": "text"},
            ]
            with patch.object(n, "PARTS_INDEX", index):
                with patch.object(n, "load_durations", return_value={}):
                    with patch.object(n, "wav_for", side_effect=lambda row, roles: root / "same.wav"):
                        with self.assertRaisesRegex(ValueError, "неоднозначные границы"):
                            n.plan_parts(rows, {}, 10)


class LockTests(unittest.TestCase):
    def test_stale_lock_with_live_or_unknown_owner_is_never_stolen(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            lock = wav.with_suffix(".lock")
            lock.write_text("12345", encoding="ascii")
            old = 1
            os.utime(lock, (old, old))
            with patch.object(n, "_pid_alive", return_value=True):
                self.assertFalse(n.claim(wav))
            self.assertEqual("12345", lock.read_text(encoding="ascii"))
            with patch.object(n, "_pid_alive", return_value=None):
                self.assertFalse(n.claim(wav))
            self.assertEqual("12345", lock.read_text(encoding="ascii"))

    def test_stale_lock_with_confirmed_dead_owner_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            lock = wav.with_suffix(".lock")
            lock.write_text("12345", encoding="ascii")
            old = 1
            os.utime(lock, (old, old))
            with patch.object(n, "_pid_alive", return_value=False):
                self.assertTrue(n.claim(wav))
            self.assertEqual(str(os.getpid()), lock.read_text(encoding="ascii"))
            n.release(wav)
            self.assertFalse(lock.exists())

    def test_claim_is_exclusive_and_release_is_owner_checked(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            self.assertTrue(n.claim(wav))
            self.assertFalse(n.claim(wav))
            n.release(wav)
            self.assertFalse(wav.with_suffix(".lock").exists())
            self.assertTrue(n.claim(wav))
            n.release(wav)


if __name__ == "__main__":
    unittest.main()
