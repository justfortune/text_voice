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


class LoadRowsFailurePolicyTests(unittest.TestCase):
    def _studio(self):
        s = studio.Studio.__new__(studio.Studio)
        s.rows = [{"id": "existing"}]
        s.rows_sig = None
        s.rows_at = 0
        return s

    def test_strict_reload_raises_instead_of_silently_reusing_old_rows(self):
        import types
        s = self._studio()
        fake_n = types.SimpleNamespace(
            CSV_PATH=Path("missing-segments.csv"),
            ROLES_PATH=Path("missing-roles.json"),
            load_rows=lambda: (_ for _ in ()).throw(OSError("segments temporarily unavailable")),
        )
        with patch.object(studio, "n", fake_n):
            with self.assertRaisesRegex(studio.Oops, "segments temporarily unavailable"):
                s.load_rows(force=True, strict=True)
        self.assertEqual([{"id": "existing"}], s.rows)

    def test_non_strict_reload_keeps_existing_runtime_fallback(self):
        import types
        s = self._studio()
        fake_n = types.SimpleNamespace(
            CSV_PATH=Path("missing-segments.csv"),
            ROLES_PATH=Path("missing-roles.json"),
            load_rows=lambda: (_ for _ in ()).throw(OSError("segments temporarily unavailable")),
        )
        with patch.object(studio, "n", fake_n):
            self.assertFalse(s.load_rows(force=True))
        self.assertEqual([{"id": "existing"}], s.rows)


class LoadRowsFailurePolicyTests(unittest.TestCase):
    def _studio(self):
        import threading
        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.takes_dir = Path("takes")
        s.rows = [{"id": "old"}]
        s.roles = {"old": "role"}
        s.keys = ["old-key"]
        s.uids = ["old-key.0"]
        s.pidx = [0]
        s.sidx = [0]
        s.idx_of = {"old-key.0": 0}
        s.id2i = {"old": 0}
        s._spans = {"old": 1}
        s._clock = (1.0, {"old": 1})
        s.stems = ["old"]
        s.base_roles = ["old"]
        s.role_cnt = {"old": 1}
        s.wavs = [Path("old.wav")]
        s.chapters = {1: [0, 0, "old"]}
        s.rows_sig = "old-signature"
        s.rows_at = 1.0
        s._abs = {4: 3}
        s.store = {"segs": {"old-key.0": {"tech": "preserve"}}, "orphans": []}
        return s

    def _source(self, voice_stems):
        import types
        row = {"id": 1, "chapter": 1, "ch_title": "Chapter", "para": 1, "kind": "text",
               "speaker": "narrator", "pause_before_ms": 0, "text": "new row"}
        fake_n = types.SimpleNamespace(
            CSV_PATH=Path("missing-segments.csv"),
            ROLES_PATH=Path("missing-roles.json"),
            load_rows=lambda: [row],
            load_roles=lambda: {},
            voice_stems=voice_stems,
        )
        return fake_n

    def test_strict_reload_rolls_back_all_derived_state_when_refresh_fails(self):
        s = self._studio()
        before = {key: getattr(s, key) for key in (
            "rows", "roles", "keys", "uids", "pidx", "sidx", "idx_of", "id2i", "_spans",
            "_clock", "stems", "base_roles", "role_cnt", "wavs", "chapters", "rows_sig", "rows_at", "_abs"
        )}
        store_before = studio.jd(s.store)
        fake_n = self._source(lambda roles: (_ for _ in ()).throw(OSError("voice stems unavailable")))
        with patch.object(studio, "n", fake_n):
            with self.assertRaisesRegex(OSError, "voice stems unavailable"):
                s.load_rows(force=True, strict=True)
        for key, value in before.items():
            self.assertEqual(value, getattr(s, key), key)
        self.assertEqual(store_before, studio.jd(s.store))

    def test_strict_reload_does_not_persist_intermediate_reanchor(self):
        s = self._studio()
        s.store = {"segs": {"missing-key.0": {"tech": "keep"}}, "orphans": [], "cues": [], "cue_orphans": []}
        s.keys, s.uids, s.idx_of = [], [], {}
        s._parts_cache = None
        s.joins_refresh = lambda: 0
        s.reanchor_cues = lambda: None
        s.save = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("intermediate save must not run"))
        s.reanchor(persist=False)
        self.assertEqual(1, len(s.store["orphans"]))

    def test_non_strict_reload_keeps_existing_runtime_fallback(self):
        import types
        s = self._studio()
        fake_n = types.SimpleNamespace(
            CSV_PATH=Path("missing-segments.csv"),
            ROLES_PATH=Path("missing-roles.json"),
            load_rows=lambda: (_ for _ in ()).throw(OSError("segments temporarily unavailable")),
            load_roles=lambda: {},
        )
        with patch.object(studio, "n", fake_n):
            self.assertFalse(s.load_rows(force=True))
        self.assertEqual([{"id": "old"}], s.rows)


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

    def test_split_refresh_failure_restores_all_state_without_retrying_refresh(self):
        s = self._studio()
        s.store["segs"]["other"] = {"sid": "sc", "join": ["missing"]}
        before = studio.jd(s.store["segs"])
        abs_before = dict(s._abs)
        undo_before = list(s.undo)
        calls = [0]

        def refresh_then_fail():
            calls[0] += 1
            s.store["segs"]["other"]["cleaned"] = True
            s._abs["corrupt"] = 99
            raise OSError("simulated refresh failure")

        s.joins_refresh = refresh_then_fail
        with self.assertRaisesRegex(OSError, "simulated refresh failure"):
            s.join_split("a")
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(abs_before, s._abs)
        self.assertEqual(undo_before, s.undo)
        self.assertEqual(1, calls[0])

    def test_join_refresh_failure_restores_all_state(self):
        s = self._join_operation_fixture()
        before = studio.jd(s.store["segs"])
        abs_before = dict(s._abs)
        undo_before = list(s.undo)
        takes_before = dict(s._takes)

        def refresh_then_fail():
            s.store["segs"]["other"]["cleaned"] = True
            s._abs["corrupt"] = 99
            raise OSError("simulated refresh failure")

        s.joins_refresh = refresh_then_fail
        with self.assertRaisesRegex(OSError, "simulated refresh failure"):
            s.join_next("a")
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(abs_before, s._abs)
        self.assertEqual(undo_before, s.undo)
        self.assertEqual(takes_before, s._takes)

    def test_join_save_failure_restores_all_state(self):
        s = self._join_operation_fixture()
        before = studio.jd(s.store["segs"])
        abs_before = dict(s._abs)
        undo_before = list(s.undo)
        takes_before = dict(s._takes)
        s.save = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated disk failure"))
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.join_next("a")
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(abs_before, s._abs)
        self.assertEqual(undo_before, s.undo)
        self.assertEqual(takes_before, s._takes)

    def _join_operation_fixture(self):
        import threading
        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.idx_of = {"a": 0, "b": 1}
        s.uids = ["a", "b"]
        s.rows = [{"id": 1, "kind": "text"}, {"id": 2, "kind": "text"}]
        s._abs = {}
        s.jobs = {}
        s.undo = [("older", "snapshot", "older change")]
        s._takes = {"cache": "preserve"}
        s.store = {"parts": [{}], "segs": {
            "a": {"sid": "sa", "tech": "before"},
            "b": {"sid": "sb"},
            "other": {"sid": "sc", "join": ["missing"]},
        }}
        s.part_of = lambda index: 1
        s.plan = lambda: [{"b": 1}]
        s.chain = lambda index: [0]
        s.E = lambda key, create=False: s.store["segs"].setdefault(key, {"sid": "s" + key})
        s.eff = lambda index: {"role": "narrator", "tech": "phrase"}
        s.src = lambda index: {"path": None, "tech": None, "imp": False}
        s.auto_tech = lambda index: ("phrase phrase", None)
        s.joins_refresh = lambda: 0
        s.save = lambda *args, **kwargs: None
        return s

    def test_join_undo_refresh_failure_restores_state_and_keeps_undo_entry(self):
        s = self._studio()
        s.store["segs"] = {
            "a": {"sid": "sa"},
            "b": {"sid": "sb"},
            "other": {"sid": "sc", "join": ["missing"]},
        }
        undo_snapshot = studio.jd({
            "a": "a", "ea": {"sid": "sa", "join": ["b"], "jwas": [{"tech": "before"}]},
            "b": "b", "eb": {"sid": "sb", "into": "a"},
        })
        s.undo = [("#join", undo_snapshot, "split")]
        before = studio.jd(s.store["segs"])
        undo_before = list(s.undo)
        calls = [0]

        def refresh_then_fail():
            calls[0] += 1
            if calls[0] == 1:
                s.store["segs"]["other"]["cleaned"] = True
                raise OSError("simulated refresh failure")
            return 0

        s.joins_refresh = refresh_then_fail
        with self.assertRaisesRegex(OSError, "simulated refresh failure"):
            s.undo_last()
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(undo_before, s.undo)
        self.assertEqual(1, calls[0])

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


class GeneralUndoSaveRollbackTests(unittest.TestCase):
    def _base(self):
        import threading
        s = studio.Studio.__new__(studio.Studio)
        s.lock = threading.RLock()
        s.idx_of = {"a": 0}
        s._abs = {}
        s.store = {"segs": {"a": {"sid": "sa", "tech": "current"}}}
        s.undo = []
        s.joins_refresh = lambda: 0
        s.rows_changed = lambda: None
        s.save = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated disk failure"))
        return s

    def test_regular_phrase_undo_restores_state_and_history_on_save_failure(self):
        s = self._base()
        old = {"sid": "sa", "tech": "previous"}
        s.store["segs"]["b"] = {"sid": "sb", "join": ["missing"]}
        s.joins_refresh = lambda: s.store["segs"]["b"].update(cleaned=True) or 0
        s.undo = [("a", old, "edit")]
        before = studio.jd(s.store["segs"])
        history = list(s.undo)
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.undo_last()
        self.assertEqual(before, studio.jd(s.store["segs"]))
        self.assertEqual(history, s.undo)

    def test_cue_undo_restores_all_keys_and_history_on_save_failure(self):
        s = self._base()
        s.store.update({"cues": [{"id": "current"}], "mix": {"gain": 0.5}})
        previous = [[{"id": "previous"}], [], {}, {"gain": 1.0}]
        s.undo = [("#cues", studio.jd(previous), "cue edit")]
        before = studio.jd(s.store)
        history = list(s.undo)
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.undo_last()
        self.assertEqual(before, studio.jd(s.store))
        self.assertEqual(history, s.undo)

    def test_insert_undo_restores_state_and_history_on_save_failure(self):
        s = self._base()
        s.store["ins"] = []
        inserted = {"id": "ins-1", "text": "inserted"}
        s.ins_rec = lambda _id: None
        s.undo = [("#ins", studio.jd({"op": "del", "id": "ins-1", "ins": inserted}), "delete insert")]
        before = studio.jd({"ins": s.store["ins"], "segs": s.store["segs"]})
        history = list(s.undo)
        with self.assertRaisesRegex(OSError, "simulated disk failure"):
            s.undo_last()
        self.assertEqual(before, studio.jd({"ins": s.store["ins"], "segs": s.store["segs"]}))
        self.assertEqual(history, s.undo)


class GlueArtifactRollbackTests(unittest.TestCase):
    def test_metadata_failure_removes_only_new_glue_artifacts(self):
        import tempfile
        import types

        class FakeArray:
            def __init__(self, values):
                self.values = list(values)
                self.rows = None

            def __len__(self):
                return len(self.values)

            def __getitem__(self, item):
                return FakeArray(self.values[item]) if isinstance(item, slice) else self.values[item]

            def reshape(self, rows, columns):
                self.rows = rows
                return self

            def __gt__(self, other):
                return [value > other for value in self.values]

        class FakeNumpy:
            @staticmethod
            def asarray(value, dtype=None):
                return value if isinstance(value, FakeArray) else FakeArray(value)

            @staticmethod
            def square(value):
                return value

            @staticmethod
            def mean(value, axis=None):
                return FakeArray([1.0] * (value.rows or len(value))) if axis == 1 else 1.0

            @staticmethod
            def sqrt(value):
                return value

            @staticmethod
            def percentile(value, percentile):
                return 1.0

            @staticmethod
            def nonzero(mask):
                return ([index for index, enabled in enumerate(mask) if enabled],)

            @staticmethod
            def zeros(count, dtype=None):
                return FakeArray([0.0] * count)

            @staticmethod
            def concatenate(values):
                return FakeArray([item for value in values for item in value.values])

        with tempfile.TemporaryDirectory() as root:
            s = studio.Studio.__new__(studio.Studio)
            s.takes_dir = Path(root)
            s._takes = {}
            s.read_audio = lambda path: (FakeArray([1.0] * 2400), 24000)
            s.row_text = lambda index: "joined phrase"
            e = {"sid": "sa"}
            fake_soundfile = types.SimpleNamespace(
                write=lambda path, audio, rate, subtype: Path(path).write_bytes(b"wav")
            )
            with patch.dict(sys.modules, {"soundfile": fake_soundfile}), \
                 patch.object(studio, "np", FakeNumpy), \
                 patch.object(studio, "n", types.SimpleNamespace(NORM_ON=False)), \
                 patch.object(studio, "write_atomic", side_effect=OSError("metadata write failed")):
                with self.assertRaisesRegex(OSError, "metadata write failed"):
                    s._glue(0, e, {"path": "first"}, {"path": "second"}, "voice", "joined phrase")

            take_dir = Path(root) / "sa"
            self.assertFalse(take_dir.exists() and list(take_dir.iterdir()))
            self.assertEqual(1, e["tseq"])


class AtomicWriteCleanupTests(unittest.TestCase):
    def test_failed_replace_preserves_previous_file_and_removes_own_temp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "edits.json"
            target.write_bytes(b"previous valid state")
            with patch.object(studio.os, "replace", side_effect=OSError("disk busy")), \
                 patch.object(studio.time, "sleep", return_value=None):
                with self.assertRaisesRegex(OSError, "disk busy"):
                    studio.write_atomic(target, b"new state")
            self.assertEqual(b"previous valid state", target.read_bytes())
            self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_successful_atomic_write_replaces_file_and_leaves_no_temp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "edits.json"
            target.write_bytes(b"previous valid state")
            studio.write_atomic(target, b"new state")
            self.assertEqual(b"new state", target.read_bytes())
            self.assertFalse(target.with_name(target.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
