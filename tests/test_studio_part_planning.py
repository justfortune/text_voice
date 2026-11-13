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
    def _clock_studio(self, root, rows):
        import threading

        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s._clock = (0.0, {})
        s._clock_sig = None
        s.rows = [{"id": str(i + 1), "text": text, "kind": "text"} for i, text in enumerate(rows)]
        s.wavs = [Path(root) / f"voice-{i + 1}.wav" for i in range(len(rows))]
        s.id2i = {str(i + 1): i for i in range(len(rows))}
        s.src = lambda i: {"path": s.wavs[i]}
        s.row_text = lambda i: s.rows[i]["text"]
        s.eff = lambda i: {"role": "narrator"}
        return s

    def test_book_clock_accumulates_time_maps_across_consecutive_parts(self):
        """A cue anchored in one part must retain its global position in the next part."""
        import types

        with tempfile.TemporaryDirectory() as td:
            parts = Path(td)
            (parts / "part_01.times.tsv").write_text(
                "00:00.0\t00:01.0\t1\tnarrator\tvoice-1\tfirst\t0.000\t1.000\n",
                encoding="utf-8",
            )
            (parts / "part_02.times.tsv").write_text(
                "00:00.0\t00:02.0\t2\tnarrator\tvoice-2\tsecond\t0.000\t2.000\n",
                encoding="utf-8",
            )
            s = self._clock_studio(parts, ["first", "second"])

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


    def test_book_clock_rejects_old_map_when_sequential_id_is_reused(self):
        import types

        with tempfile.TemporaryDirectory() as td:
            parts = Path(td)
            (parts / "part_01.times.tsv").write_text(
                "00:00.0\t00:01.0\t1\tnarrator\told-voice\told edition text\t0.000\t1.000\n",
                encoding="utf-8",
            )
            s = self._clock_studio(parts, ["new edition text"])
            s.wavs[0] = parts / "new-voice.wav"

            with patch.object(studio, "n", types.SimpleNamespace(PARTS=parts)):
                clock = s.book_clock()

        self.assertEqual({}, clock)

    def test_book_clock_invalidates_cache_when_time_map_is_replaced(self):
        import os
        import types

        with tempfile.TemporaryDirectory() as td:
            parts = Path(td)
            time_map = parts / "part_01.times.tsv"
            line = "00:00.0\t00:01.0\t1\tnarrator\tvoice-1\tfirst\t0.000\t1.000\n"
            time_map.write_text(line, encoding="utf-8")
            s = self._clock_studio(parts, ["first"])

            with patch.object(studio, "n", types.SimpleNamespace(PARTS=parts)):
                first = s.book_clock()
                changed = line.replace("1.000", "2.000")
                time_map.write_text(changed, encoding="utf-8")
                st = time_map.stat()
                os.utime(time_map, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
                second = s.book_clock()

        self.assertEqual((0.0, 1.0), first[0])
        self.assertEqual((0.0, 2.0), second[0])

    def test_finite_sfx_continues_across_part_boundary_with_remaining_duration(self):
        s = studio.Studio.__new__(studio.Studio)
        s.idx_of = {"before": 0, "inside": 1}
        s.cue_lead = lambda cue, i, a: 2.5
        s.cue_file = lambda cue: None
        cue = {"uid": "before", "est": 5.0, "kind": "sfx", "loop": False}

        result = s.cue_times(cue, {"inside": (0.0, 1.0)}, 1, 1, 3.0)

        self.assertIsNotNone(result)
        self.assertEqual(0.0, result[0])
        self.assertEqual(2.5, result[1])
        self.assertAlmostEqual(2.5, result[2])
        self.assertFalse(result[3])

    def test_finite_sfx_that_finished_before_next_part_is_not_replayed(self):
        s = studio.Studio.__new__(studio.Studio)
        s.idx_of = {"before": 0, "inside": 1}
        s.cue_lead = lambda cue, i, a: 6.0
        s.cue_file = lambda cue: None
        cue = {"uid": "before", "est": 5.0, "kind": "sfx", "loop": False}

        result = s.cue_times(cue, {"inside": (0.0, 1.0)}, 1, 1, 3.0)

        self.assertIsNone(result)

    def test_render_version_changes_when_uid_anchor_changes_even_if_audio_key_is_same(self):
        import threading
        import types

        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.rows = [{"kind": "text", "chapter": 1, "ch_title": "chapter"}]
        s.roles = {"roles": {"narrator": {"tempo": 1.0, "gain": 1.0}}}
        s.base_roles = ["narrator"]
        s.uids = ["same-text.0"]
        s._abs = {}
        s.speed = 1.0
        s.rule_index = lambda: {}
        s.eff = lambda i, ridx: {"pause": 100, "after": 0.0}
        s.src = lambda i: {"path": Path("same-audio.wav")}
        s.eq_for = lambda i: {}
        s.proc_key = lambda i, eq, ef, sc: "shared-audio-key"
        s.asm_sig = lambda: "same-assembly"

        with patch.object(studio, "n", types.SimpleNamespace(TEMPO_G=1.0, PAUSE_SCALE=1.0)):
            first = s.timeline(0, 0, False)[0]["ver"]
            s.uids[0] = "same-text.1"
            second = s.timeline(0, 0, False)[0]["ver"]

        self.assertNotEqual(first, second)



    def test_timeline_keeps_old_edition_anchors_if_book_reloads_during_processing(self):
        import threading
        import types

        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.rows = [{"id": 1, "kind": "head", "chapter": 1, "ch_title": "Original Chapter", "text": "Chapter"}]
        s.base_roles = ["narrator"]
        s.uids = ["original-heading.0"]
        s._abs = {}
        s.speed = 1.0
        s.roles = {"roles": {"narrator": {"tempo": 1.0, "gain": 1.0}}}
        s.rule_index = lambda: {}
        s.eff = lambda i, ridx: {"pause": 0, "after": 0.0, "role": "narrator", "tempo": 1.0}
        s.src = lambda i: {"path": Path("original.wav"), "kind": "draft", "role": "narrator"}
        s.eq_for = lambda i: {}
        s.proc_key = lambda i, eq, ef, sc: "same-audio-key"
        s.asm_sig = lambda: "same-assembly"
        observed = {}

        def process(items, op=None, row_snapshots=None, roles_snapshot=None):
            observed["row"] = dict(row_snapshots[0])
            observed["roles"] = roles_snapshot["roles"]["narrator"]["tempo"]
            # Simulate a concurrent edition reload after the expensive work has begun.
            s.rows = [{"kind": "text", "chapter": 99, "ch_title": "Edited Chapter", "text": "Edited"}]
            s.uids = ["edited-text.0"]
            s.roles = {"roles": {"narrator": {"tempo": 1.4, "gain": 0.8}}}
            return {"same-audio-key": [0.1, 0.2]}

        s._proc_many = process
        fake_np = types.SimpleNamespace(
            zeros=lambda count, dtype=None: [0.0] * count,
            concatenate=lambda arrays: [sample for array in arrays for sample in array],
        )
        fake_n = types.SimpleNamespace(
            TEMPO_G=1.0,
            PAUSE_SCALE=1.0,
            PAUSE={"chapter_end": 0},
        )

        with patch.object(studio, "np", fake_np), patch.object(studio, "n", fake_n):
            meta, audio = s.timeline(0, 0, True)

        self.assertEqual("Original Chapter", observed["row"]["ch_title"])
        self.assertEqual(1.0, observed["roles"])
        self.assertEqual([["original-heading.0", 0.0, 0.0]], meta["segs"])
        self.assertEqual([[0.0, "Original Chapter"]], meta["chapters"])
        self.assertEqual([0.1, 0.2], audio)

    def test_proc_many_passes_captured_row_and_roles_to_worker(self):
        with tempfile.TemporaryDirectory() as td:
            s = studio.Studio.__new__(studio.Studio)
            s.cache_dir = Path(td)
            s.threads = 1
            s.rows = [{"text": "new edition"}]
            s.roles = {"roles": {"narrator": {"tempo": 2.0}}}
            source = Path(td) / "shared.wav"
            captured = {}

            def fake_proc(i, eq, ef, sc, key, row_snapshot=None, roles_snapshot=None):
                captured["row"] = row_snapshot
                captured["tempo"] = roles_snapshot["roles"]["narrator"]["tempo"]
                return [1.0, 2.0]

            s.proc = fake_proc
            items = [(0, {"role": "narrator"}, {"path": source}, {}, "shared-key", 0)]
            old_rows = {0: {"text": "captured old edition"}}
            old_roles = {"roles": {"narrator": {"tempo": 1.25}}}

            result = s._proc_many(items, row_snapshots=old_rows, roles_snapshot=old_roles)

        self.assertEqual("captured old edition", captured["row"]["text"])
        self.assertEqual(1.25, captured["tempo"])
        self.assertEqual([1.0, 2.0], result["shared-key"])



    def test_timeline_version_tracks_export_speed_and_row_metadata(self):
        import threading
        import types

        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.rows = [{"id": 11, "kind": "text", "chapter": 1, "ch_title": "Chapter",
                   "para": 1, "speaker": "narrator", "text": "same text"}]
        s.roles = {"roles": {"narrator": {"tempo": 1.0, "gain": 1.0}}}
        s.base_roles = ["narrator"]
        s.uids = ["same-text.0"]
        s._abs = {}
        s.speed = 1.0
        s.fmt = "m4b"
        s.bitrate = "96k"
        s.title = "Book"
        s.author = "Author"
        s.rule_index = lambda: {}
        s.eff = lambda i, ridx: {"pause": 100, "after": 0.0, "role": "narrator",
                                 "tech": "same text", "tech_src": "base", "locked": False,
                                 "tempo": 1.0, "hits": []}
        s.src = lambda i: {"path": Path("same-audio.wav")}
        s.eq_for = lambda i: {}
        s.proc_key = lambda i, eq, ef, sc: "shared-audio-key"
        s.asm_sig = lambda: "same-assembly"

        with patch.object(studio, "n", types.SimpleNamespace(TEMPO_G=1.0, PAUSE_SCALE=1.0)):
            baseline = s.timeline(0, 0, False)[0]["ver"]
            s.speed = 1.25
            speed_changed = s.timeline(0, 0, False)[0]["ver"]
            s.speed = 1.0
            s.rows[0]["para"] = 2
            paragraph_changed = s.timeline(0, 0, False)[0]["ver"]

        self.assertNotEqual(baseline, speed_changed)
        self.assertNotEqual(baseline, paragraph_changed)


    def test_export_aborts_on_edition_change_without_replacing_previous_final_file(self):
        import threading

        class FakeOp:
            def __init__(self, *_args):
                self.note = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stage(self, *_args):
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final_dir = root / "final"
            final_dir.mkdir()
            output = final_dir / "part_01.m4b"
            output.write_bytes(b"previous-valid-export")
            s = studio.Studio.__new__(studio.Studio)
            s.lock = threading.RLock()
            s.rlock = threading.RLock()
            s.final_parts = final_dir
            s.fmt = "m4b"
            s.title = "Book"
            s.rows = [{"ch_title": "Chapter"}]
            s.uids = ["same-boundary.0"]
            s.open_part = lambda _k: {"a": 0, "b": 0}
            timeline_calls = []

            def timeline(_a, _b, want_audio, _op=None):
                timeline_calls.append(want_audio)
                if want_audio:
                    return {"ver": "old-edition", "sr": 24000, "chapters": [], "segs": []}, [0.1, 0.2]
                return {"ver": "new-edition"}, None

            def encode(_audio, _sr, _chapters, staged_path, _name):
                staged_path.write_bytes(b"new-but-stale-export")
                return 12.0

            s.timeline = timeline
            s._encode = encode
            s._export_mixes = lambda *_a, **_k: self.fail("mixing must not run for a stale edition")

            with patch.object(studio, "Op", FakeOp), patch.object(studio, "active_rms", return_value=0.5):
                with self.assertRaisesRegex(studio.Oops, "изменились во время экспорта"):
                    s.export_part(1)

            self.assertEqual([True, False], timeline_calls)
            self.assertEqual(b"previous-valid-export", output.read_bytes())
            self.assertEqual([output], list(final_dir.iterdir()))




if __name__ == "__main__":
    unittest.main()
