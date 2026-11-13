# -*- coding: utf-8 -*-
"""Regression tests for data-integrity fixes in nox_audiobook.py."""
import os
import sys
import tempfile
import threading
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
            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "PAUSE", {"para": 1000}), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \
                 patch.object(n, "duration_of", return_value=40.0):
                with self.assertRaisesRegex(ValueError, "другой редакции"):
                    n.plan_parts(rows, {}, 2.0)

    def test_missing_frozen_row_ids_are_not_silently_replanned(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": i + 10, "text": f"new-{i}", "pause_before_ms": 1000, "kind": "text"}
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
            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "PAUSE", {"para": 1000}), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \
                 patch.object(n, "duration_of", return_value=40.0):
                with self.assertRaisesRegex(ValueError, "другой редакции"):
                    n.plan_parts(rows, {}, 2.0)

    def test_frozen_part_signature_detects_middle_segment_edits(self):
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
            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "PAUSE", {"para": 1000}), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", side_effect=lambda row, roles: wavs[str(row["id"])]), \
                 patch.object(n, "duration_of", return_value=40.0):
                with self.assertRaisesRegex(ValueError, "содержимое части изменилось"):
                    n.plan_parts(rows, {}, 2.0)

    def test_invalid_speed_is_rejected_before_planning(self):
        import math
        for speed in (0, -1, float("inf"), float("nan")):
            with self.subTest(speed=speed):
                with self.assertRaisesRegex(ValueError, "скорость сборки"):
                    n.plan_parts([], {}, 2.0, speed=speed)


class BookEditionSnapshotTests(unittest.TestCase):
    def _fixture(self, root):
        work = Path(root) / "work"
        parts = Path(root) / "parts"
        work.mkdir()
        parts.mkdir()
        csv_path = work / "segments.csv"
        roles_path = work / "roles.json"
        index_path = work / "parts.json"
        csv_path.write_text("id;chapter;text\n1;1;old edition\n", encoding="utf-8")
        roles_path.write_text('{"roles":{"narrator":{"voice":"original"}}}', encoding="utf-8")
        index_path.write_text('[{"n":1,"start_id":"1","end_id":"1","start":"a","end":"a"}]', encoding="utf-8")
        (parts / "part_01.times.tsv").write_text(
            "00:00.0\t00:02.0\t1\tnarrator\ta\told edition\t0.000\t2.000\n",
            encoding="utf-8",
        )
        (parts / "part_01.m4b").write_bytes(b"previous-book-audio")
        return work, parts, csv_path, roles_path, index_path

    def test_previous_edition_snapshot_preserves_csv_roles_index_and_time_maps(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            work, parts, csv_path, roles_path, index_path = self._fixture(td)
            with patch.object(n, "WORK", work), \
                 patch.object(n, "PARTS", parts), \
                 patch.object(n, "CSV_PATH", csv_path), \
                 patch.object(n, "ROLES_PATH", roles_path), \
                 patch.object(n, "PARTS_INDEX", index_path):
                snapshot = n._snapshot_current_edition()
                original_csv = (snapshot / "segments.csv").read_text(encoding="utf-8")
                original_roles = (snapshot / "roles.json").read_text(encoding="utf-8")
                original_index = (snapshot / "parts.json").read_text(encoding="utf-8")
                original_map = (snapshot / "parts" / "part_01.times.tsv").read_text(encoding="utf-8")
                manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))

                csv_path.write_text("id;chapter;text\n9;2;edited edition\n", encoding="utf-8")
                roles_path.write_text('{"roles":{"narrator":{"voice":"new"}}}', encoding="utf-8")
                index_path.write_text("[]", encoding="utf-8")
                (parts / "part_01.times.tsv").write_text("changed map", encoding="utf-8")

                second = n._snapshot_current_edition()

            self.assertNotEqual(snapshot, second)
            self.assertIn("old edition", original_csv)
            self.assertIn("original", original_roles)
            self.assertEqual("1", json.loads(original_index)[0]["start_id"])
            self.assertIn("old edition", original_map)
            self.assertEqual(b"previous-book-audio", (parts / "part_01.m4b").read_bytes())
            self.assertEqual(False, manifest["audio_copied"])
            self.assertEqual([{"path": "parts/part_01.m4b", "size": len(b"previous-book-audio")}],
                             manifest["outputs_kept_in_place"])

    def test_corrupt_existing_edition_snapshot_stops_before_source_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            work, parts, csv_path, roles_path, index_path = self._fixture(td)
            with patch.object(n, "WORK", work), \
                 patch.object(n, "PARTS", parts), \
                 patch.object(n, "CSV_PATH", csv_path), \
                 patch.object(n, "ROLES_PATH", roles_path), \
                 patch.object(n, "PARTS_INDEX", index_path):
                snapshot = n._snapshot_current_edition()
                (snapshot / "segments.csv").write_text("damaged snapshot", encoding="utf-8")
                original_csv = csv_path.read_bytes()
                original_roles = roles_path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "архив прежней редакции повреждён"):
                    n._snapshot_current_edition()

            self.assertEqual(original_csv, csv_path.read_bytes())
            self.assertEqual(original_roles, roles_path.read_bytes())

    def test_atomic_segments_write_failure_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "segments.csv"
            target.write_text("previous complete edition", encoding="utf-8")
            rows = [{"id": 1, "chapter": 1, "ch_title": "A", "para": 0, "kind": "text",
                     "speaker": "narrator", "conf": "high", "pause_before_ms": 0, "text": "new"}]

            with patch.object(n, "CSV_PATH", target), patch.object(n.os, "replace", side_effect=OSError("simulated")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    n._atomic_write_segments(rows)

            self.assertEqual("previous complete edition", target.read_text(encoding="utf-8"))
            self.assertEqual([target], list(Path(td).iterdir()))


class PartsIndexRecoveryTests(unittest.TestCase):
    def test_corrupt_parts_index_is_preserved_and_never_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "parts.json"
            damaged = '{"n": 1,'
            index.write_text(damaged, encoding="utf-8")

            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}):
                with self.assertRaisesRegex(ValueError, "повреждён"):
                    n.plan_parts([], {}, 2.0)

            self.assertEqual(damaged, index.read_text(encoding="utf-8"))

    def test_non_list_parts_index_is_rejected_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "parts.json"
            original = '{"parts": []}'
            index.write_text(original, encoding="utf-8")

            with patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}):
                with self.assertRaisesRegex(ValueError, "неизвестную структуру"):
                    n.plan_parts([], {}, 2.0)

            self.assertEqual(original, index.read_text(encoding="utf-8"))

    def test_orphan_existing_part_file_is_not_silently_reused_without_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parts = root / "parts"
            parts.mkdir()
            index = root / "work" / "parts.json"
            orphan = parts / "part_01.m4b"
            orphan.write_bytes(b"old-edition-audio")
            wav = root / "voice.wav"
            wav.write_bytes(b"wav")
            row = {"id": 1, "speaker": "narrator", "text": "new edition",
                   "pause_before_ms": 0, "kind": "text", "ch_title": "Chapter"}

            with patch.object(n, "PARTS", parts), \
                 patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", return_value=wav), \
                 patch.object(n, "duration_of", return_value=120.0):
                with self.assertRaisesRegex(ValueError, "не подтверждает содержимое"):
                    n.build_parts([row], {}, 2.0, "Book", "Author", "96k", quiet=True)

            self.assertEqual(b"old-edition-audio", orphan.read_bytes())
            self.assertFalse(index.exists())

    def test_rebuilding_early_part_does_not_truncate_validated_index_suffix(self):
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parts = root / "parts"
            parts.mkdir()
            index = root / "work" / "parts.json"
            wavs = [root / "voice-1.wav", root / "voice-2.wav"]
            for wav in wavs:
                wav.write_bytes(b"wav")
            rows = [
                {"id": i + 1, "speaker": "narrator", "text": f"line-{i + 1}",
                 "pause_before_ms": 0, "kind": "text", "ch_title": "Chapter"}
                for i in range(2)
            ]
            keys = [wav.stem for wav in wavs]
            signatures = [
                hashlib.sha256(key.encode("utf-8")).hexdigest()
                for key in keys
            ]
            part_two = parts / "part_02.m4b"
            part_two.write_bytes(b"second-edition-audio")
            saved = [
                {"n": i + 1, "start_id": str(i + 1), "end_id": str(i + 1),
                 "start": keys[i], "end": keys[i], "sig": signatures[i],
                 "build_sig": f"build-{i + 1}",
                 "output_sha256": ("0" * 64 if i == 1 else None)}
                for i in range(2)
            ]
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps(saved), encoding="utf-8")

            def encode(_audio, _sr, _chapters, out, *_args, **_kwargs):
                out.write_bytes(b"rebuilt-first-part")
                return 60.0

            with patch.object(n, "PARTS", parts), \
                 patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "PAUSE", {"para": 1000}), \
                 patch.object(n, "wav_for", side_effect=lambda row, _roles: wavs[int(row["id"]) - 1]), \
                 patch.object(n, "duration_of", return_value=120.0), \
                 patch.object(n, "assemble", return_value=(None, 24000, [], None)), \
                 patch.object(n, "_part_build_signature", side_effect=lambda _rows, _roles, a, _b, *_args: f"build-{a + 1}"), \
                 patch.object(n, "encode_m4b", side_effect=encode):
                with self.assertRaisesRegex(ValueError, "контрольная сумма не совпадает"):
                    n.build_parts(rows, {}, 2.0, "Book", "Author", "96k", quiet=True)

            persisted = json.loads(index.read_text(encoding="utf-8"))
            self.assertEqual(2, len(persisted))
            self.assertEqual(signatures[1], persisted[1]["sig"])
            self.assertEqual("0" * 64, persisted[1]["output_sha256"])
            self.assertTrue((parts / "part_01.m4b").is_file())
            self.assertEqual(b"second-edition-audio", part_two.read_bytes())


    def test_existing_part_is_not_reused_after_encoder_configuration_changes(self):
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parts = root / "parts"
            parts.mkdir()
            index = root / "parts.json"
            output = parts / "part_01.m4b"
            output.write_bytes(b"audio-encoded-at-old-speed")
            wav = root / "voice.wav"
            wav.write_bytes(b"wav")
            row = {"id": 1, "speaker": "narrator", "text": "same text",
                   "pause_before_ms": 0, "kind": "text", "ch_title": "Chapter"}
            sig = hashlib.sha256(wav.stem.encode("utf-8")).hexdigest()
            index.write_text(json.dumps([{
                "n": 1, "start_id": "1", "end_id": "1",
                "start": wav.stem, "end": wav.stem, "sig": sig,
                "build_sig": "old-speed-and-settings",
                "output_sha256": n._file_sha256(output),
            }]), encoding="utf-8")

            with patch.object(n, "PARTS", parts), \
                 patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", return_value=wav), \
                 patch.object(n, "duration_of", return_value=120.0), \
                 patch.object(n, "_part_build_signature", return_value="new-speed-and-settings"):
                with self.assertRaisesRegex(ValueError, "другими параметрами"):
                    n.build_parts([row], {}, 2.0, "Book", "Author", "96k", quiet=True, speed=1.25)

            self.assertEqual(b"audio-encoded-at-old-speed", output.read_bytes())

    def test_legacy_part_index_without_build_signature_is_not_silently_reused(self):
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parts = root / "parts"
            parts.mkdir()
            index = root / "parts.json"
            output = parts / "part_01.m4b"
            output.write_bytes(b"legacy-audio")
            wav = root / "voice.wav"
            wav.write_bytes(b"wav")
            row = {"id": 1, "speaker": "narrator", "text": "same text",
                   "pause_before_ms": 0, "kind": "text", "ch_title": "Chapter"}
            sig = hashlib.sha256(wav.stem.encode("utf-8")).hexdigest()
            index.write_text(json.dumps([{
                "n": 1, "start_id": "1", "end_id": "1",
                "start": wav.stem, "end": wav.stem, "sig": sig,
                "output_sha256": n._file_sha256(output),
            }]), encoding="utf-8")

            with patch.object(n, "PARTS", parts), \
                 patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", return_value=wav), \
                 patch.object(n, "duration_of", return_value=120.0), \
                 patch.object(n, "_part_build_signature", return_value="current-build-signature"):
                with self.assertRaisesRegex(ValueError, "без подписи параметров сборки"):
                    n.build_parts([row], {}, 2.0, "Book", "Author", "96k", quiet=True)

            self.assertEqual(b"legacy-audio", output.read_bytes())


    def test_part_output_checksum_mismatch_is_not_silently_accepted(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parts = root / "parts"
            parts.mkdir()
            index = root / "parts.json"
            output = parts / "part_01.m4b"
            output.write_bytes(b"edited-or-damaged-audio")
            wav = root / "voice.wav"
            wav.write_bytes(b"wav")
            row = {"id": 1, "speaker": "narrator", "text": "same text",
                   "pause_before_ms": 0, "kind": "text", "ch_title": "Chapter"}
            import hashlib
            sig = hashlib.sha256(wav.stem.encode("utf-8")).hexdigest()
            index.write_text(json.dumps([{
                "n": 1, "start_id": "1", "end_id": "1",
                "start": wav.stem, "end": wav.stem, "sig": sig,
                "build_sig": "build-1",
                "output_sha256": "0" * 64,
            }]), encoding="utf-8")

            with patch.object(n, "PARTS", parts), \
                 patch.object(n, "PARTS_INDEX", index), \
                 patch.object(n, "DUR_CACHE", root / "durations.json"), \
                 patch.object(n, "load_durations", return_value={}), \
                 patch.object(n, "wav_for", return_value=wav), \
                 patch.object(n, "duration_of", return_value=120.0), \
                 patch.object(n, "_part_build_signature", return_value="build-1"):
                with self.assertRaisesRegex(ValueError, "контрольная сумма не совпадает"):
                    n.build_parts([row], {}, 2.0, "Book", "Author", "96k", quiet=True)

            self.assertEqual(b"edited-or-damaged-audio", output.read_bytes())


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
            self.assertTrue(lock.read_text(encoding="ascii").startswith(str(os.getpid()) + ":"))
            n.release(wav)
            self.assertFalse(lock.exists())

    def test_release_does_not_remove_a_replacement_lock_with_another_token(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            lock = wav.with_suffix(".lock")
            self.assertTrue(n.claim(wav))
            own = lock.read_text(encoding="ascii")
            replacement = f"{os.getpid()}:{'f' * 32}"
            self.assertNotEqual(own, replacement)
            lock.write_text(replacement, encoding="ascii")

            n.release(wav)

            self.assertEqual(replacement, lock.read_text(encoding="ascii"))

    def test_concurrent_claims_and_stale_reclamation_are_serialized(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            lock = wav.with_suffix(".lock")
            lock.write_text("12345", encoding="ascii")
            os.utime(lock, (1, 1))
            start = threading.Barrier(3)
            attempted = threading.Barrier(3)
            release_owner = threading.Event()
            results = []

            def contender():
                start.wait(timeout=5)
                acquired = n.claim(wav)
                results.append(acquired)
                attempted.wait(timeout=5)
                if acquired:
                    release_owner.wait(timeout=5)
                    n.release(wav)

            with patch.object(n, "_pid_alive", return_value=False):
                workers = [threading.Thread(target=contender) for _ in range(2)]
                for worker in workers:
                    worker.start()
                start.wait(timeout=5)
                attempted.wait(timeout=5)
                self.assertEqual([False, True], sorted(results))
                release_owner.set()
                for worker in workers:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())

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
