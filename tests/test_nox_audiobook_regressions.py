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


class LockTests(unittest.TestCase):
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
