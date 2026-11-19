"""Acceptance tests at the actual Studio HTTP/state/history boundaries.

All media belongs to a TemporaryDirectory. No installed book, GPU, cloud or
external process is touched. WAV reads use the stdlib wave fixture reader.
"""
import copy
import csv
import http.client
import io
import json
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import patch

from backend_studio import StudioFixture, studio, ft, row, row_uids, wav
from nox_audio_archive import archive_stem
from nox_freeze import validate_protection, fixed_document, read_protection


class StatePublicationTests(StudioFixture):
    def lock_first(self):
        s = self.s
        s.store["segs"][s.uids[0]]["locked"] = {"at": "today", "take": "t001"}
        s.store["parts"] = [{"n": 1, "last": s.uids[0], "locked": "today"}]

    def test_locked_state_publishes_consistent_edits_and_fixed_pair(self):
        s = self.s
        self.lock_first()
        s.save("закреплена часть")
        fixed = json.loads((s.dir / "fixed_parts.json").read_text())
        edits = json.loads((s.dir / "edits.json").read_text())
        state = validate_protection(s.rows, fixed, edits)
        self.assertEqual(state.protected_indices, frozenset({0}))
        self.assertEqual(fixed["parts"][0]["b_uid"], s.uids[0])

    def test_second_document_failure_rolls_back_both_and_memory(self):
        s = self.s
        paths = [s.dir / "edits.json", s.dir / "fixed_parts.json"]
        original = {p: p.read_bytes() for p in paths}
        self.lock_first()
        atomic = ft.atomic_bytes
        failed = []
        def fail_fixed(path, data):
            if Path(path) == paths[1] and not failed:
                failed.append(True)
                raise PermissionError("injected sharing violation")
            return atomic(path, data)
        with patch.object(ft, "atomic_bytes", side_effect=fail_fixed):
            with self.assertRaises(studio.Oops):
                s.save("закреплена часть")
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertNotIn("locked", s.store["segs"][s.uids[0]])
        self.assertEqual(s.store["parts"], [])

    def test_changed_unchanged_fixed_document_is_not_overwritten(self):
        s = self.s
        (s.dir / "fixed_parts.json").write_text('{"external":true,"parts":[]}')
        s.store["segs"][s.uids[0]]["tech"] = "правка"
        with self.assertRaises(studio.Oops):
            s.save("правка")
        self.assertEqual(json.loads((s.dir / "fixed_parts.json").read_text())["external"], True)
        self.assertNotIn("tech", s.store["segs"][s.uids[0]])

    def test_prepare_shared_lock_blocks_commit_before_metadata_changes(self):
        s = self.s
        before = (s.dir / "edits.json").read_bytes()
        with ft.directory_lock(s.dir / "transactions"):
            with self.assertRaises(studio.Oops):
                s.choose_take(s.uids[0], "t002")
        self.assertEqual((s.dir / "edits.json").read_bytes(), before)
        self.assertEqual(s.store["segs"][s.uids[0]]["take"], "t001")

    def test_unlock_part_removes_phrase_and_part_guard_in_same_publication(self):
        s = self.s
        self.lock_first()
        s.store["segs"][s.uids[0]]["locked"].update(role="narrator", tech=s.row_text(0), tempo=1., pause=300, after=0.)
        s.save("закрепление")
        s.unlock_part(1)
        fixed = json.loads((s.dir / "fixed_parts.json").read_text())
        edits = json.loads((s.dir / "edits.json").read_text())
        self.assertFalse(edits["parts"][0].get("locked"))
        self.assertFalse(edits["segs"][s.uids[0]].get("locked"))
        self.assertEqual(validate_protection(s.rows, fixed, edits).protected_indices, frozenset())

    def test_project_can_restore_quarantined_files_and_undo_still_restores_remainder(self):
        s = self.s
        original = s.take_path("s000001", "t002").read_bytes()
        s.clean_takes(False)
        self.assertIsNotNone(s._trash_find("s000001", "t002"))
        self.assertTrue(s._trash_back("s000001", "t002", "wav"))
        s.undo_last()  # WAV already restored, JSON is restored transactionally.
        self.assertEqual(s.take_path("s000001", "t002").read_bytes(), original)
        self.assertIn("t002", s.takes_of(s.store["segs"][s.uids[0]]))

    def test_old_project_counter_does_not_reuse_quarantined_take_number(self):
        s = self.s
        s.delete_take(s.uids[0], "t002")
        e = s.store["segs"][s.uids[0]]; e["tseq"] = 0
        self.assertEqual(s.new_tid(e), "t003")

    def test_nested_take_metadata_update_and_missing_wav_are_seen(self):
        s = self.s
        e = s.store["segs"][s.uids[0]]
        self.assertEqual(s.takes_of(e)["t001"]["tech"], s.row_text(0))
        metadata = s.take_path(e["sid"], "t001").with_suffix(".json")
        data = json.loads(metadata.read_text()); data["tech"] = "Новый технический текст."
        metadata.write_text(json.dumps(data))
        self.assertEqual(s.takes_of(e)["t001"]["tech"], "Новый технический текст.")
        metadata.with_suffix(".wav").unlink()
        self.assertNotIn("t001", s.takes_of(e))


class LauncherTests(StudioFixture):
    def test_pipeline_preparation_does_not_spawn_extra_editor_model(self):
        s = self.s
        s.go_proc = types.SimpleNamespace(poll=lambda: None)
        s.engine_proc = s.engine2 = None; s.engine_note = ""; s.workers = lambda: []
        base = studio.Studio.__mro__[1]
        with patch.object(base, "engine_start", return_value={"explicit": True}) as start:
            view = s.engine_start()
            self.assertTrue(view["pipeline_starting"]); self.assertFalse(view["ready"])
            start.assert_not_called()
            self.assertEqual(s.engine_start(force=True), {"explicit": True})
            start.assert_called_once_with(True)

    def test_book_envfast_and_explicit_interpreter_are_respected(self):
        s = self.s
        interpreter = s.root / "envfast/Scripts/python.exe"
        interpreter.parent.mkdir(parents=True); interpreter.write_bytes(b"fixture")
        with patch.dict("os.environ", {"NOX_TOOL_PY": ""}):
            self.assertEqual(s.tool_py("fast"), str(interpreter))
        with patch.dict("os.environ", {"NOX_TOOL_PY": str(s.root / "missing.exe")}):
            with self.assertRaises(studio.Oops): s.tool_py("fast")
        with patch.dict("os.environ", {"NOX_TOOL_PY": str(interpreter)}):
            self.assertEqual(s.tool_py("fast"), str(interpreter))

    def test_actual_engine_spawn_preserves_installed_book_envfast_fix(self):
        s = self.s
        interpreter = s.root / "envfast/Scripts/python.exe"
        interpreter.parent.mkdir(parents=True); interpreter.write_bytes(b"fixture")
        fast = s.root / "nox_fast.py"; fast.write_text("# studio_queue capability")
        (s.root / "work/fast_args.json").write_text(json.dumps({"model": "fixture/model", "max_new_tokens": 1500}))
        runtime = types.SimpleNamespace(WORK=s.root / "work", SETTINGS={})
        process = types.SimpleNamespace(pid=12345)
        with patch.object(studio, "n", runtime), \
             patch.object(studio.sys, "executable", str(s.root / "studio-env/pythonw.exe")), \
             patch.dict("os.environ", {"NOX_TOOL_PY": "", "PYTHONHOME": "unrelated", "PYTHONPATH": "unrelated"}), \
             patch.object(studio.subprocess, "Popen", return_value=process) as spawn:
            actual, command = s._engine_spawn(9)
        self.assertIs(actual, process)
        self.assertEqual(command[:2], [str(interpreter), str(fast)])
        self.assertEqual(command[command.index("--worker") + 1], "9")
        self.assertEqual(command[command.index("--max-new-tokens") + 1], "1500")
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(s.root))
        self.assertNotIn("PYTHONHOME", spawn.call_args.kwargs["env"])
        self.assertNotIn("PYTHONPATH", spawn.call_args.kwargs["env"])

    def test_installed_kit_record_is_authoritative_and_stale_record_does_not_fallback(self):
        s = self.s
        kit = s.root / "release/kit"; kit.mkdir(parents=True)
        (kit / "go_kniga1a.ps1").write_text("fixture")
        record = s.root / "work/nox_release.json"
        record.write_text(json.dumps({"kit_dir": str(kit)}))
        self.assertEqual(s.release_kit_dir(), kit)
        (kit / "go_kniga1a.ps1").unlink()
        with self.assertRaises(studio.Oops): s.release_kit_dir()


class LegacyPartSelectionTests(StudioFixture):
    def setUp(self):
        super().setUp()
        s = self.s
        s.auto_tech = lambda i, ridx=None: (s.rows[i]["text"], [])
        self.uid = s.uids[0]
        self.selected = self.add_take("s000001", "t003", s.row_text(0))
        s.store["segs"][self.uid].update(take="t003", tseq=3)
        s.store["parts"] = [{"n": 1, "last": self.uid, "locked": "previous accepted part"}]
        self.persist_fixture()
        (s.dir / "fixed_parts.json").write_text(json.dumps(fixed_document(s.rows, s.store)))
        s._fixed_sha = ft.digest(s.dir / "fixed_parts.json")
        s._part_selected = read_protection(s.root, s.rows, check_audio=True).part_selected_uids

    def test_load_preserves_real_selected_record_without_fabricating_lock(self):
        s = self.s
        original = {name: (s.dir / name).read_bytes() for name in ("edits.json", "fixed_parts.json")}
        s.load_store()
        for name, data in original.items():
            self.assertEqual((s.dir / name).read_bytes(), data)
        self.assertEqual(s.store["segs"][self.uid]["take"], "t003")
        self.assertNotIn("locked", s.store["segs"][self.uid])
        self.assertEqual(s._part_selected_guard_uids(), {self.uid})
        self.assertFalse(studio.Studio.eff(s, 0)["locked"])

    def test_cleanup_preserves_selected_pair_inside_fixed_part(self):
        s = self.s
        selected_bytes = {p: p.read_bytes() for p in (self.selected, self.selected.with_suffix(".json"))}
        fixed = (s.dir / "fixed_parts.json").read_bytes()
        result = s.clean_takes(False)
        self.assertEqual(result["takes"], 2)
        for p, raw in selected_bytes.items():
            self.assertEqual(p.read_bytes(), raw)
        self.assertEqual((s.dir / "fixed_parts.json").read_bytes(), fixed)
        edits = json.loads((s.dir / "edits.json").read_text())
        self.assertEqual(edits["segs"][self.uid]["take"], "t003")
        self.assertNotIn("locked", edits["segs"][self.uid])
        self.assertEqual(read_protection(s.root, s.rows, check_audio=True).part_selected_uids, {self.uid})

    def test_manual_set_and_choose_remain_available_with_part_protection(self):
        s = self.s
        fixed = (s.dir / "fixed_parts.json").read_bytes()
        s.set_seg(self.uid, {"tempo": 1.12, "mark": "manual review"})
        s.choose_take(self.uid, "t002")
        edits = json.loads((s.dir / "edits.json").read_text())
        self.assertEqual(edits["segs"][self.uid]["take"], "t002")
        self.assertEqual(edits["segs"][self.uid]["tempo"], 1.12)
        self.assertNotIn("locked", edits["segs"][self.uid])
        self.assertTrue(edits["parts"][0]["locked"])
        self.assertEqual((s.dir / "fixed_parts.json").read_bytes(), fixed)
        self.assertEqual(read_protection(s.root, s.rows, check_audio=True).part_selected_uids, {self.uid})
        self.assertTrue(self.selected.exists())

    def test_automatic_silence_repair_reports_but_keeps_selected_take(self):
        s = self.s
        original = (s.dir / "edits.json").read_bytes()
        with patch.object(s, "take_dead", side_effect=lambda tk: bool(tk and tk.get("tid") == "t003")):
            result = s.heal_silent()
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(result["part_selected"], 1)
        self.assertEqual((s.dir / "edits.json").read_bytes(), original)
        self.assertEqual(s.store["segs"][self.uid]["take"], "t003")
        s.choose_take(self.uid, "t002")
        self.assertEqual(s.store["segs"][self.uid]["take"], "t002")

    def test_automatic_reanchor_does_not_guess_part_selected_placement(self):
        s = self.s
        before = copy.deepcopy(s.store)
        s.rows = [row(0, "Другой контекст."), row(1, "Первая фраза."), row(2, "Другой конец."),
                  row(3, "Первая фраза."), row(4, "Ещё один конец.")]
        s.uids = row_uids(s.rows); s.keys = [u.split(".")[0] for u in s.uids]
        s.idx_of = {uid: i for i, uid in enumerate(s.uids)}
        s.reanchor_cues = lambda: None
        with self.assertRaises(studio.Oops):
            s.reanchor(persist=False)
        self.assertEqual(s.store, before)
        self.assertTrue(self.selected.exists())

    def test_last_anchored_insertion_keeps_auto_guard_after_save(self):
        s = self.s
        ins_uid = "ins000009"
        text = "Авторская вставка после границы."
        s.store["ins"] = [{"id": ins_uid, "after": self.uid, "after_id": "1", "ord": 1,
                           "text": text, "role": "narrator"}]
        inserted = dict(s.rows[0], id="1a", text=text, ins=ins_uid)
        s.rows.insert(1, inserted); s.uids.insert(1, ins_uid); s.keys.insert(1, ins_uid)
        s.idx_of = {uid: i for i, uid in enumerate(s.uids)}
        s.sidx = [0, 2, 3]; s.pidx = [0, None, 1, 2]
        s.base_roles.insert(1, "narrator"); s.wavs.insert(1, s.root / "unused-insertion-draft.wav")
        s.store["segs"][ins_uid] = {"sid": "s000009", "take": "t007", "tseq": 8, "ctx": s.ctx_of(1)}
        selected = self.add_take("s000009", "t007", text)
        self.add_take("s000009", "t008", text)
        original_audio = selected.read_bytes()
        self.persist_fixture()
        fixed = fixed_document(s._pipeline_rows(), s.store)
        (s.dir / "fixed_parts.json").write_text(json.dumps(fixed))
        s._fixed_sha = ft.digest(s.dir / "fixed_parts.json")
        s._part_selected = read_protection(s.root, s._pipeline_rows(), check_audio=True).part_selected_uids
        self.assertIn(ins_uid, s._part_selected)
        s.save("сохранены метаданные с вставкой после последней исходной фразы")
        self.assertEqual(s._part_selected, {self.uid, ins_uid})
        with patch.object(Path, "read_bytes", side_effect=AssertionError("metadata guard must not read files")):
            self.assertEqual(s._derive_part_selected_uids(), {self.uid, ins_uid})
        with patch.object(s, "take_dead", side_effect=lambda tk: bool(tk and tk.get("tid") == "t007")):
            report = s.heal_silent()
        self.assertEqual(report["fixed"], 0)
        self.assertEqual(report["part_selected"], 1)
        self.assertEqual(s.store["segs"][ins_uid]["take"], "t007")
        self.assertNotIn("locked", s.store["segs"][ins_uid])
        self.assertEqual(selected.read_bytes(), original_audio)


class PipelineHistoryTests(StudioFixture):
    def snapshot(self, records=None, revision="rev01"):
        s = self.s
        source = s.root / "work/wav/previous.wav"
        wav(source)
        source.with_suffix(".txt").write_text(s.row_text(1), encoding="utf-8")
        folder = s.root / "work/freeze_backups" / revision
        (folder / "work").mkdir(parents=True)
        table = io.StringIO()
        writer = csv.DictWriter(table, delimiter=";", fieldnames=list(s.rows[0]))
        writer.writeheader(); writer.writerows(s.rows)
        (folder / "work/segments.csv").write_text(table.getvalue(), encoding="utf-8")
        (folder / "work/roles.json").write_text(json.dumps(s.roles), encoding="utf-8")
        records = records or [{"uid": s.uids[1], "index": 1, "id": 2, "role": "narrator", "text": s.row_text(1),
                              "wav": "work/wav/previous.wav", "sha256": ft.digest(source), "size": source.stat().st_size,
                              "retained_wav": "work/retained_audio/fallback/previous.wav",
                              "sidecars": {"txt": {"path": "work/wav/previous.txt", "sha256": ft.digest(source.with_suffix('.txt'))}}}]
        doc = {"v": 1, "revision": revision, "state": "committed", "catalog_ready": True,
               "files": {rel: ft.digest(folder / rel) for rel in ("work/segments.csv", "work/roles.json")},
               "pipeline_audio": records}
        path = folder / "manifest.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return source, path, doc

    def test_snapshot_source_follows_verified_archive_and_adopts_provenance(self):
        s = self.s
        source, manifest, doc = self.snapshot()
        original = source.read_bytes()
        history = archive_stem(s.root, source.stem, "test replacement", target_row={"text": "THIS IS NOT THE SOURCE"})
        self.assertFalse(source.exists())
        candidates = s.old_voice_for(s.uids[1], with_info=True)
        self.assertEqual(candidates["errors"], [])
        self.assertEqual(candidates["total"], 1)
        old = candidates["items"][0]
        self.assertEqual(old["source_raw"], s.row_text(1))
        self.assertTrue(old["auto_safe"])
        self.assertEqual(s.old_voice_path(old["sid"], old["tid"]).read_bytes(), original)
        result = s.old_voice_adopt(s.uids[1], old["sid"], old["tid"], choose=True)
        e = s.store["segs"][s.uids[1]]
        path = s.take_path(e["sid"], result["tid"])
        meta = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(meta["source_raw"], s.row_text(1))
        self.assertEqual(meta["source_revision"], "rev01")
        self.assertNotIn("THIS IS NOT THE SOURCE", json.dumps(meta))

    def test_unknown_archive_is_manual_only_and_never_uses_target_text(self):
        s = self.s
        source = s.root / "work/wav/unknown.wav"; wav(source)
        source.with_suffix(".txt").write_text(s.row_text(1))
        archive_stem(s.root, source.stem, "replacement", target_row={"text": s.row_text(1), "speaker": "narrator"})
        old = s.old_voice_for(s.uids[1])[0]
        self.assertIsNone(old["source_raw"])
        self.assertIsNone(old["role"])
        self.assertFalse(old["auto_safe"])
        self.assertFalse(old["exact"])
        self.assertEqual(s.old_voice_auto(1)["got"], 0)
        result = s.old_voice_adopt(s.uids[1], old["sid"], old["tid"])
        e = s.store["segs"][s.uids[1]]
        meta = json.loads(s.take_path(e["sid"], result["tid"]).with_suffix(".json").read_text())
        self.assertFalse(meta["provenance_complete"])
        self.assertIsNone(meta["source_raw"])

    def test_changed_snapshot_csv_rejects_complete_manifest(self):
        s = self.s
        _, manifest, doc = self.snapshot()
        (manifest.parent / "work/segments.csv").write_text("broken")
        result = s.old_voice_for(s.uids[1], with_info=True)
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_nested_snapshot_damage_invalidates_cached_catalog(self):
        s = self.s
        _, manifest, _ = self.snapshot()
        self.assertEqual(len(s.pipeline_old_voices()), 1)
        (manifest.parent / "work/roles.json").write_text('{"roles":{}}')
        self.assertEqual(s.pipeline_old_voices(), [])
        self.assertEqual(len(s._old_voice_errors), 1)

    def test_corrupt_late_record_does_not_leak_earlier_valid_record(self):
        s = self.s
        _, manifest, doc = self.snapshot()
        broken = dict(doc["pipeline_audio"][0], uid="wrong_uid")
        doc["pipeline_audio"].append(broken)
        manifest.write_text(json.dumps(doc))
        result = s.old_voice_for(s.uids[1], with_info=True)
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_identical_wav_with_distinct_placements_does_not_guess_archive_origin(self):
        s = self.s
        source, manifest, doc = self.snapshot()
        doc["pipeline_audio"].append(dict(doc["pipeline_audio"][0], uid=s.uids[2], index=2, id=3, text=s.row_text(2)))
        manifest.write_text(json.dumps(doc))
        archive_stem(s.root, source.stem, "replacement")
        unknown = [v for v in s.pipeline_old_voices() if v["sid"].startswith("h")]
        self.assertEqual(len(unknown), 1)
        self.assertTrue(unknown[0]["provenance_ambiguous"])
        self.assertFalse(unknown[0]["provenance_complete"])
        self.assertIsNone(unknown[0]["source_raw"])

    def test_manual_text_edit_is_never_replaced_automatically(self):
        s = self.s
        self.snapshot()
        target = s.uids[1]
        s.store["segs"][target] = {"tech": "Авторский вариант текста", "ctx": s.ctx_of(1)}
        s.eff = lambda i: {"role": "narrator", "tech": s.store["segs"].get(s.uids[i], {}).get("tech") or s.row_text(i),
                           "tempo": 1., "pause": 300, "after": 0.}
        self.persist_fixture()
        candidate = s.old_voice_for(target)[0]
        self.assertTrue(candidate["exact"])
        self.assertFalse(candidate["tech_match"])
        self.assertFalse(candidate["auto_safe"])
        self.assertEqual(s.old_voice_auto(1)["got"], 0)
        self.assertEqual(s.store["segs"][target]["tech"], "Авторский вариант текста")


class ReanchorTests(StudioFixture):
    def repeated_rows(self):
        s = self.s
        old_uid = s.uids[0]
        s.rows = [row(0, "Другой контекст."), row(1, "Первая фраза."), row(2, "Другой конец."),
                  row(3, "Первая фраза."), row(4, "Ещё один конец.")]
        s.uids = row_uids(s.rows); s.keys = [u.split(".")[0] for u in s.uids]
        s.idx_of = {u: i for i, u in enumerate(s.uids)}; s.pidx=list(range(5)); s.sidx=list(range(5))
        s.reanchor_cues = lambda: None
        return old_uid

    def test_repeated_zero_neighbor_match_goes_to_orphan_without_moving_files(self):
        s = self.s
        old_uid = self.repeated_rows()
        path = s.take_path("s000001", "t001"); original = path.read_bytes()
        s.reanchor(persist=False)
        self.assertNotIn(old_uid, s.store["segs"])
        self.assertEqual(s.store["orphans"][-1]["uid"], old_uid)
        self.assertEqual(path.read_bytes(), original)

    def test_repeated_zero_neighbor_locked_match_refuses_and_keeps_state(self):
        s = self.s
        old_uid = self.repeated_rows()
        s.store["segs"][old_uid]["locked"] = {"at": "today", "take": "t001"}
        before = copy.deepcopy(s.store)
        with self.assertRaises(studio.Oops):
            s.reanchor(persist=False)
        self.assertEqual(s.store, before)


class HttpTests(StudioFixture):
    def setUp(self):
        super().setUp()
        self.s.token = "fixture-token"
        self.previous = studio.S
        studio.S = self.s
        self.server = studio.Server(("127.0.0.1", 0), studio.H)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        studio.S = self.previous

    def request(self, path, body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Origin": "http://127.0.0.1"}
        if token:
            headers["X-Nox-Token"] = self.s.token
        connection.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse(); status, raw = response.status, response.read(); connection.close()
        return status, json.loads(raw)

    def test_trim_empty_rejected_and_real_spans_roundtrip_then_undo(self):
        s = self.s
        original = (s.dir / "edits.json").read_bytes()
        code, result = self.request("/api/take_trim", {"uid": s.uids[0], "tid": "t001", "spans": []})
        self.assertEqual(code, 400); self.assertIn("error", result)
        self.assertEqual((s.dir / "edits.json").read_bytes(), original)
        code, _ = self.request("/api/take_trim", {"uid": s.uids[0], "tid": "t001", "spans": [[0,.3],[.6,1.]]})
        self.assertEqual(code, 200)
        self.assertEqual(s.store["segs"][s.uids[0]]["trims"]["t001"], [[0.,.3],[.6,1.]])
        code, _ = self.request("/api/undo", {})
        self.assertEqual(code, 200); self.assertNotIn("trims", s.store["segs"][s.uids[0]])

    def test_token_guard_precedes_delete(self):
        code, result = self.request("/api/take", {"uid": self.s.uids[0], "delete": "t001"}, token=False)
        self.assertEqual(code, 403)
        self.assertTrue(self.s.take_path("s000001", "t001").exists())

    def test_old_voice_pagination_is_real_complete_dto(self):
        s = self.s
        for index in range(5):
            sid = "s" + str(10 + index)
            self.add_take(sid, "t001", s.row_text(1))
            s.store["orphans"].append({"uid": "old" + sid, "e": {"sid": sid}, "at": str(index)})
        code, first = self.request("/api/old_voice?uid=" + s.uids[1] + "&limit=2")
        self.assertEqual(code, 200); self.assertEqual(first["total"], 5); self.assertEqual(first["next"], 2)
        code, second = self.request("/api/old_voice?uid=" + s.uids[1] + "&limit=2&offset=2")
        self.assertEqual(code, 200); self.assertEqual(second["next"], 4)
        self.assertNotEqual(first["items"][0]["sid"], second["items"][0]["sid"])
        self.assertTrue(all(v["ambiguous"] for v in first["items"]))

    def test_external_sound_pagination_and_refresh_route(self):
        s = self.s
        folder = s.root / "game"; folder.mkdir()
        for index in range(3): wav(folder / f"WolfAttack{index}.wav")
        s.kit_catalog = lambda: []; s.kit_active = lambda: None
        s.sound_root_add(str(folder), "Игра")
        code, result = self.request("/api/sounds?limit=2")
        self.assertEqual(code, 200); self.assertEqual(len(result["sounds"]), 2)
        self.assertEqual(result["total"], 3); self.assertEqual(result["next"], 2)
        code, tree = self.request("/api/sound_refresh", {})
        self.assertEqual(code, 200); self.assertEqual(sum(section["n"] for section in tree["sections"]), 3)


if __name__ == "__main__":
    unittest.main()
