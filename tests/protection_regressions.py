"""Regression tests for real Stage 21/Studio contracts; no TTS/GPU required."""
from __future__ import annotations
import ast
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from collections import Counter
from unittest.mock import patch, Mock

KIT = Path(__file__).resolve().parents[1] / "kit"
sys.path.insert(0, str(KIT))
import nox_freeze as nf

FIELDS = ["id", "chapter", "ch_title", "para", "kind", "speaker", "conf", "pause_before_ms", "text"]


def rows(*texts):
    return [dict(id=i + 1, chapter=1, ch_title="Глава", para=i, kind="narr", speaker="narrator",
                 conf="high", pause_before_ms=400, text=t) for i, t in enumerate(texts)]


def state(rr, last=0, *, part=True, joined=False):
    u = nf.row_uids(rr)
    st = {"v": 1, "segs": {}, "parts": [], "ins": [], "rules": [], "cues": []}
    for i in range(last + 1):
        st["segs"][u[i]] = {"sid": f"s{i}", "take": "t001",
                             "locked": {"at": "2026-11-19 12:00:00", "take": "t001", "role": "narrator", "tech": rr[i]["text"]}}
    if joined:
        st["segs"][u[0]]["join"] = u[1:last + 1]
        for uid in u[1:last + 1]:
            st["segs"][uid]["into"] = u[0]
            st["segs"][uid]["locked"] = {"at": "2026-11-19 12:00:00", "head": u[0]}
    if part:
        st["parts"] = [{"n": 1, "last": u[last], "last_i": last, "last_p": last,
                        "locked": "2026-11-19 12:00:00", "opened": "2026-11-18", "export": None, "eq": None}]
    return nf.fixed_document(rr, st), st


def csv_data(rr):
    f = io.StringIO(newline="")
    w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";")
    w.writeheader()
    w.writerows(rr)
    return f.getvalue().encode("utf-8")


def put(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return p


def fixture(root, rr, fixed=None, edits=None):
    put(root, "work/segments.csv", csv_data(rr))
    put(root, "work/roles.json", {"roles": {"narrator": {"count": len(rr)}}, "aliases": {}})
    put(root, "book.docx", b"original manuscript")
    if fixed is not None: put(root, "studio/fixed_parts.json", fixed)
    if edits is not None:
        put(root, "studio/edits.json", edits)
        for uid, e in edits["segs"].items():
            if e.get("locked") and not e["locked"].get("head"):
                base = f"studio/takes/{e['sid']}/{e['sid']}_t001"
                put(root, base + ".wav", b"RIFF" + b"audio" * 20)
                put(root, base + ".json", {"tid": "t001", "cover": e.get("join", [])})


def generated_module(root):
    ps = (KIT / "prep_kniga1a.ps1").read_text(encoding="utf-8-sig")
    src = ps.split("$patchPy = @'", 1)[1].split("\n'@", 1)[0].lstrip("\n")
    script, runtime = root / "patch.py", root / "nox_audiobook.py"
    script.write_text(src, encoding="utf-8")
    shutil.copyfile(KIT.parent / "base/nox_audiobook.py", runtime)
    out = subprocess.run([sys.executable, str(script), str(runtime)], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stdout + out.stderr)
    module = types.ModuleType("generated_nox_test")
    module.__file__ = str(runtime)
    exec(compile(runtime.read_text(encoding="utf-8"), str(runtime), "exec"), module.__dict__)
    return module


class Contracts(unittest.TestCase):
    def test_real_main_schema_and_joined_children(self):
        rr = rows("А.", "Б.", "В.")
        fx, st = state(rr, 1, joined=True)
        p = nf.validate_protection(rr, fx, st)
        self.assertEqual(p.ranges, ((0, 1, 1),))
        self.assertEqual(p.protected_indices, {0, 1})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture(root, rr, fx, st)
            # No child WAV was created: only the head owns the joined recording.
            self.assertFalse((root / "studio/takes/s1").exists())
            nf.read_protection(root, rr, check_audio=True)

    def test_conflicting_real_last_is_not_accepted(self):
        rr = rows("А.", "Б.", "В.")
        fx, st = state(rr, 1)
        fx["parts"][0].update(b=0, b_uid=nf.row_uids(rr)[0])
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, st)

    def test_missing_last_does_not_fall_back_to_last_p_or_last_i(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        st["parts"][0]["last"] = "missing.0"
        with self.assertRaisesRegex(nf.ProtectionError, "last UID"):
            nf.validate_protection(rr, fx, st)

    def test_overlapping_fixed_ranges_rejected(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        fx["parts"].append(dict(fx["parts"][0], n=2))
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, st)

    def test_non_prefix_locked_parts_are_valid_main_contract(self):
        rr = rows("А.", "Б.", "В.")
        _, st = state(rr, 1)
        u = nf.row_uids(rr)
        st["segs"].pop(u[0])
        st["parts"] = [dict(n=1, last=u[0], locked=None), dict(n=2, last=u[1], locked="2026-11-19")]
        fx = nf.fixed_document(rr, st)
        self.assertEqual(nf.validate_protection(rr, fx, st).ranges, ((1, 1, 2),))

    def test_individual_lock_without_fixed_part_is_protected(self):
        rr = rows("А.", "Б.", "В.")
        fx, st = state(rr, 1, part=False)
        st["segs"].pop(nf.row_uids(rr)[0])
        self.assertEqual(nf.validate_protection(rr, fx, st).protected_indices, {1})
        merged = nf.merge_locked_prefix(rr, rows("А.", "Б.", "Новый хвост."), fx, st)
        self.assertEqual([r["text"] for r in merged.rows], ["А.", "Б.", "Новый хвост."])
        self.assertEqual(merged.preserved_segments, 2)

    def test_partial_paragraph_is_retained_as_whole(self):
        rr = rows("Первое предложение.", "Второе предложение.", "Хвост.")
        rr[1]["para"] = 0
        fx, st = state(rr)
        new = rows("Первое предложение. Второе предложение.", "Другой хвост.")
        new[0]["para"], new[1]["para"] = 10, 11
        result = nf.merge_locked_prefix(rr, new, fx, st)
        self.assertEqual(result.preserved_segments, 2)
        self.assertEqual(result.rows[1]["text"], "Второе предложение.")
        self.assertEqual(result.rows[0]["para"], 10)
        self.assertEqual(nf.row_uids(result.rows)[:2], nf.row_uids(rr)[:2])

    def test_paragraph_number_shift_is_safe_content_change_is_not(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        new = copy.deepcopy(rr)
        for r in new: r["para"] += 7
        self.assertEqual(nf.merge_locked_prefix(rr, new, fx, st).rows[0]["para"], 7)
        new[0]["text"] = "Изменено."
        with self.assertRaises(nf.ProtectionError): nf.merge_locked_prefix(rr, new, fx, st)

    def test_inserted_identical_paragraph_is_ambiguous(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        with self.assertRaises(nf.ProtectionError): nf.merge_locked_prefix(rr, rows("А.", "А.", "Б."), fx, st)

    def test_unchanged_repeated_paragraphs_remain_compatible(self):
        rr = rows("Да.", "Да.", "Хвост.")
        fx, st = state(rr, 1)
        self.assertEqual(nf.merge_locked_prefix(rr, copy.deepcopy(rr), fx, st).preserved_segments, 2)

    def test_truthy_corrupt_lock_rejected(self):
        rr = rows("А.")
        fx, st = state(rr)
        st["segs"][nf.row_uids(rr)[0]]["locked"] = "yes"
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, st)

    def test_broken_join_rejected(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr, 1, joined=True)
        st["segs"][nf.row_uids(rr)[1]]["into"] = "another-head"
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, st)

    def test_active_tail_cue_cannot_be_rebound_to_equal_text_elsewhere(self):
        rr = rows("Начало.", "Да.", "Один контекст.", "Да.", "Другой контекст.")
        fx, st = state(rr)
        st["cues"] = [{"uid": nf.row_uids(rr)[1], "id": "cue"}]
        new = rows("Начало.", "Один контекст.", "Да.", "Да.", "Другой контекст.")
        with self.assertRaises(nf.ProtectionError): nf.merge_locked_prefix(rr, new, fx, st)

    def test_missing_or_malformed_fixed_document_never_means_empty(self):
        rr = rows("А.")
        fx, st = state(rr)
        for bad in ({"parts": {}}, {"parts": None}, {}, {"parts": False}, None):
            with self.subTest(bad=bad), self.assertRaises(nf.ProtectionError):
                nf.validate_protection(rr, bad, st)
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, None)

    def test_unanchored_insertion_cannot_use_numeric_fallback(self):
        rr = rows("А.")
        fx, st = state(rr)
        st["ins"] = [{"id": "ins1", "after": "lost.0", "after_id": "1", "text": "вставка"}]
        with self.assertRaises(nf.ProtectionError): nf.validate_protection(rr, fx, st)

    def test_locked_studio_insertions_before_and_after_pipeline_rows(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        for num, anchor in enumerate(("", nf.row_uids(rr)[0])):
            uid = f"ins{num}"
            st["ins"].append(dict(id=uid, after=anchor, after_id="0" if not anchor else "1", ord=num,
                                  text="Авторская вставка.", role="narrator", pause=420))
            st["segs"][uid] = dict(sid=f"i{num}", take="t001", locked=dict(take="t001", at="2026-11-19"))
        result = nf.validate_protection(rr, fx, st)
        self.assertEqual(result.locked_uids, {nf.row_uids(rr)[0], "ins0", "ins1"})
        with tempfile.TemporaryDirectory() as td:
            fixture(Path(td), rr, fx, st)
            nf.read_protection(td, rr, check_audio=True)
        merged = nf.merge_locked_prefix(rr, rows("А.", "Новый хвост."), fx, st)
        self.assertEqual(merged.rows[0]["text"], "А.")

    def test_locked_part_requires_its_insertions_to_be_locked_too(self):
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        st["ins"] = [dict(id="ins1", after=nf.row_uids(rr)[0], text="Добавлено.")]
        with self.assertRaisesRegex(nf.ProtectionError, "авторская вставка"):
            nf.validate_protection(rr, fx, st)

    def test_insertion_only_part_is_reported_without_numeric_guessing(self):
        rr = rows("А.")
        fx, st = state(rr)
        st["ins"] = [dict(id="ins1", after="", text="Добавлено.")]
        st["parts"][0]["last"] = "ins1"
        with self.assertRaisesRegex(nf.ProtectionError, "авторскую вставку"):
            nf.validate_protection(rr, fx, st)


class FilesystemTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rr = rows("Начало.", "Хвост.")
        self.fx, self.st = state(self.rr)
        fixture(self.root, self.rr, self.fx, self.st)

    def test_corrupt_json_blocks_before_changes(self):
        old = (self.root / "work/segments.csv").read_bytes()
        put(self.root, "studio/edits.json", b'{"segs":')
        with self.assertRaises(nf.ProtectionError):
            with nf.preparation(self.root): pass
        self.assertEqual((self.root / "work/segments.csv").read_bytes(), old)
        self.assertFalse((self.root / "work/.nox_prepare.lock").exists())

    def test_checked_backup_and_atomic_publication_keep_audio(self):
        old = (self.root / "work/segments.csv").read_bytes()
        audio = next((self.root / "studio/takes").rglob("*.wav"))
        wav_bytes = audio.read_bytes()
        with nf.preparation(self.root) as tx:
            new = nf.commit_prepared_rows(self.root, rows("Начало.", "Новое окончание."), FIELDS)
            snapshot = tx["snapshot"]
        self.assertEqual((snapshot / "work/segments.csv").read_bytes(), old)
        self.assertEqual(audio.read_bytes(), wav_bytes)
        self.assertEqual(new[1]["text"], "Новое окончание.")
        self.assertEqual(json.loads((snapshot / "manifest.json").read_text())["state"], "committed")

    def test_concurrent_edits_change_cancels_csv_publication(self):
        old = (self.root / "work/segments.csv").read_bytes()
        with nf.preparation(self.root):
            altered = copy.deepcopy(self.st)
            altered["note"] = "made concurrently"
            put(self.root, "studio/edits.json", altered)
            with self.assertRaises(nf.ProtectionError):
                nf.commit_prepared_rows(self.root, rows("Начало.", "Новое окончание."), FIELDS)
        self.assertEqual((self.root / "work/segments.csv").read_bytes(), old)
        self.assertEqual(json.loads((self.root / "studio/edits.json").read_text())["note"], "made concurrently")

    def test_audio_change_during_prepare_cancels(self):
        old = (self.root / "work/segments.csv").read_bytes()
        with nf.preparation(self.root):
            wav = next((self.root / "studio/takes").rglob("*.wav"))
            wav.write_bytes(b"changed" * 20)
            with self.assertRaises(nf.ProtectionError):
                nf.commit_prepared_rows(self.root, rows("Начало.", "Новое окончание."), FIELDS)
        self.assertEqual((self.root / "work/segments.csv").read_bytes(), old)

    def test_partial_publish_rolls_back_roles_manuscript_and_csv(self):
        before = {rel: (self.root / rel).read_bytes() for rel in ("book.docx", "work/roles.json", "work/segments.csv")}
        real = nf._atomic_bytes
        def fail_csv(path, data):
            if Path(path) == self.root / "work/segments.csv":
                raise OSError("injected disk error")
            return real(path, data)
        with nf.preparation(self.root) as tx:
            source = put(self.root, "new.docx", b"new manuscript")
            tx["source_files"] = [(source, source.read_bytes())]
            with patch.object(nf, "_atomic_bytes", fail_csv), self.assertRaises(OSError):
                nf.commit_prepared_rows(self.root, rows("Начало.", "Новый хвост."), FIELDS,
                                        roles={"roles": {"narrator": {"count": 10}}})
            self.assertEqual(tx["manifest"]["state"], "rolled_back")
        for rel, data in before.items(): self.assertEqual((self.root / rel).read_bytes(), data)

    def test_stale_preparation_marker_is_not_silently_removed(self):
        marker = put(self.root, "work/.nox_prepare.lock", {"pid": 9999999, "token": "old"})
        with self.assertRaises(nf.ProtectionError):
            with nf.preparation(self.root): pass
        self.assertEqual(json.loads(marker.read_text())["token"], "old")
        with self.assertRaises(nf.ProtectionError): nf.read_protection(self.root, self.rr)

    def test_missing_audio_sidecar_is_detected(self):
        next((self.root / "studio/takes").rglob("*.json")).unlink()
        with self.assertRaises(nf.ProtectionError): nf.read_protection(self.root, self.rr, check_audio=True)

    def test_retained_pipeline_bundle_keeps_both_sidecar_revisions(self):
        wav = put(self.root, "work/wav/example.wav", b"RIFF" + b"audio" * 20)
        side = put(self.root, "work/wav/example.txt", b"old spoken text")
        target = nf.preserve_pipeline_bundle(self.root, wav)
        first_sha = hashlib.sha256(side.read_bytes()).hexdigest()
        side.write_bytes(b"different spoken text")
        nf.preserve_pipeline_bundle(self.root, wav)
        self.assertEqual((target / "example.wav").read_bytes(), wav.read_bytes())
        self.assertEqual((target / "sidecar_versions" / first_sha / "example.txt").read_bytes(), b"old spoken text")

    def test_read_protection_is_read_only(self):
        def files():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        before = files()
        nf.read_protection(self.root, self.rr, check_audio=True)
        self.assertEqual(files(), before)

    def test_preparation_excludes_studio_transactions(self):
        from nox_file_transactions import directory_lock, TransactionError
        with nf.preparation(self.root):
            with self.assertRaises(TransactionError):
                with directory_lock(self.root / "studio/transactions"): pass
        with directory_lock(self.root / "studio/transactions"):
            with self.assertRaises(nf.ProtectionError):
                with nf.preparation(self.root): pass

    def test_explicit_recovery_of_failed_rollback_checks_hashes(self):
        old_roles = (self.root / "work/roles.json").read_bytes()
        old_csv = (self.root / "work/segments.csv").read_bytes()
        real = nf._atomic_bytes
        def fail_publish_and_rollback(path, payload):
            if Path(path) == self.root / "work/segments.csv":
                raise OSError("publish failed")
            if Path(path) == self.root / "work/roles.json" and payload == old_roles:
                raise OSError("rollback failed")
            return real(path, payload)
        with self.assertRaises(nf.ProtectionError):
            with nf.preparation(self.root):
                with patch.object(nf, "_atomic_bytes", fail_publish_and_rollback):
                    nf.commit_prepared_rows(self.root, rows("Начало.", "Хвост изменён."), FIELDS,
                                            roles={"roles": {"narrator": {"count": 100}}})
        marker = self.root / "work/.nox_prepare.lock"
        self.assertTrue(marker.exists())
        with self.assertRaisesRegex(nf.ProtectionError, "процесс подготовки"):
            nf.recover_preparation(self.root)
        with patch.object(nf, "_pid_alive", return_value=False):
            result = nf.recover_preparation(self.root)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual((self.root / "work/roles.json").read_bytes(), old_roles)
        self.assertEqual((self.root / "work/segments.csv").read_bytes(), old_csv)
        self.assertFalse(marker.exists())


class PipelineIntegration(unittest.TestCase):
    def test_historical_a534_chunk_order_and_extreme_limits(self):
        text = ("«Ровно ничего» — это не для красного словца. В двадцать пять лет я умел менять масло, колёса и тормозные "
                "колодки, мог на спор с завязанными глазами перебрать двигатель от «шевроле», умел жарить яичницу на одной "
                "конфорке так, чтобы желток оставался целым, знал телепрограмму на неделю вперёд и был совершенно уверен, "
                "что магия бывает только в кино, а кино бывает только по кабельному.")
        with tempfile.TemporaryDirectory() as td:
            n = generated_module(Path(td))
            self.assertTrue(n.chunk_text(text, 190)[0].startswith("«Ровно ничего»"))
            self.assertEqual(" ".join(n.chunk_text(text, 190)).split(), text.split())
            cases = [text, "— Стой! — сказал он. — Я здесь; не уходи: это важно.", "Да. Нет. Да.",
                     "A" * 200 + " конец.", "0 1 01 — 10.", "", "Первая.\nВторая.\tТретья."]
            for value in cases:
                for limit in (1, 2, 3, 19, 190, 300):
                    chunks = n.chunk_text(value, limit)
                    self.assertEqual(re.sub(r"\s+", "", "".join(chunks)), re.sub(r"\s+", "", value))
                    self.assertTrue(all(0 < len(c) <= int(limit * 1.5) for c in chunks))
            for invalid in (0, -1, True, 1.5):
                with self.assertRaises(ValueError): n.chunk_text("Текст.", invalid)

    def test_assembly_invalidation_keeps_all_audio_and_nonprefix_fixed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rr = rows("А.", "Б.")
            _, st = state(rr, 1)
            uid = nf.row_uids(rr)
            st["segs"].pop(uid[0])
            st["parts"] = [dict(n=1, last=uid[0], locked=None), dict(n=2, last=uid[1], locked="2026-11-19")]
            fx = nf.fixed_document(rr, st)
            fixture(root, rr, fx, st)
            put(root, "work/parts.json", [{"n": 1, "sig": "old1"}, {"n": 2, "sig": "old2"}])
            names = ("parts/part_01.mp3", "parts/part_02.mp3", "book.mp3", "parts_zvuk/v1/part_02.mp3",
                     "cloud/part_01.mp3", "cloud/part_02.mp3", "cloud/book.mp3")
            for name in names: put(root, name, ("old " + name).encode())
            n = generated_module(root)
            args = types.SimpleNamespace(parts=19, format="mp3", bitrate="96k", title="Книга", author="Автор", speed=1.)
            before = {name: (root / name).read_bytes() for name in names}
            n._asm_setup(args, root / "cloud", {"part_01.mp3": "old"})
            for name, data in before.items(): self.assertEqual((root / name).read_bytes(), data)
            index = json.loads((root / "work/parts.json").read_text())
            self.assertIsNone(index[0]["sig"])
            self.assertEqual(index[1]["sig"], "old2")
            self.assertTrue(index[1]["fixed"])
            put(root, "studio/fixed_parts.json", b"bad json")
            before_index = (root / "work/parts.json").read_bytes()
            with self.assertRaises(nf.ProtectionError): n._asm_setup(args, root / "cloud", {})
            self.assertEqual((root / "work/parts.json").read_bytes(), before_index)

    def test_real_segment_cli_retains_prefix_and_rolls_back_changed_manuscript(self):
        from docx import Document
        ps = (KIT / "prep_kniga1a.ps1").read_text(encoding="utf-8-sig")
        patch_src = ps.split("$patchPy = @'", 1)[1].split("\n'@", 1)[0].lstrip("\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime, script = root / "nox_audiobook.py", root / "patch.py"
            script.write_text(patch_src, encoding="utf-8")
            shutil.copyfile(KIT.parent / "base/nox_audiobook.py", runtime)
            shutil.copyfile(KIT / "nox_freeze.py", root / "nox_freeze.py")
            r = subprocess.run([sys.executable, str(script), str(runtime)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            source = root / "source.docx"
            def manuscript(first, second):
                doc = Document()
                doc.add_paragraph(first)
                doc.add_paragraph(second)
                doc.save(source)
            def segment(max_chars):
                return subprocess.run([sys.executable, str(runtime), "segment", str(source), "--max-chars", str(max_chars)],
                                      capture_output=True, text=True)
            manuscript("Первая фраза. Вторая фраза.", "Старый хвост книги.")
            r = segment(20)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            old_rows = nf._csv_rows((root / "work/segments.csv").read_bytes())
            fx, st = state(old_rows)
            put(root, "studio/edits.json", st)
            put(root, "studio/fixed_parts.json", fx)
            put(root, "studio/takes/s0/s0_t001.wav", b"RIFF" + b"audio" * 20)
            put(root, "studio/takes/s0/s0_t001.json", {"tid": "t001"})
            manuscript("Первая фраза. Вторая фраза.", "Новый хвост книги.")
            r = segment(300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            retained = nf._csv_rows((root / "work/segments.csv").read_bytes())
            self.assertEqual(nf.row_uids(retained)[0], nf.row_uids(old_rows)[0])
            self.assertEqual(retained[-1]["text"], "Новый хвост книги.")
            self.assertEqual((root / "book.docx").read_bytes(), source.read_bytes())
            before = {rel: (root / rel).read_bytes() for rel in ("book.docx", "work/segments.csv", "work/roles.json",
                                                               "studio/edits.json", "studio/fixed_parts.json")}
            manuscript("Чужое начало.", "Новый хвост книги.")
            r = segment(300)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("закреплённый префикс", r.stderr)
            for rel, data in before.items(): self.assertEqual((root / rel).read_bytes(), data)

    def test_generated_stage21_preserves_real_fixed_contract(self):
        # Exercise the actual prep patch on the pinned base, not a handwritten
        # facsimile of fixed_parts. Only pure protection functions are executed.
        base = KIT.parent / "base/nox_audiobook.py"
        ps = (KIT / "prep_kniga1a.ps1").read_text(encoding="utf-8-sig")
        patch_src = ps.split("$patchPy = @'", 1)[1].split("\n'@", 1)[0].lstrip("\n")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "patch.py"
            script.write_text(patch_src, encoding="utf-8")
            runtime = root / "nox_audiobook.py"
            shutil.copyfile(base, runtime)
            process = subprocess.run([sys.executable, str(script), str(runtime)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            tree = ast.parse(runtime.read_text(encoding="utf-8"))
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("fixed_parts", "fixed_set", "fixed_prefix")]
            env = {"_nox_read_protection": nf.read_protection, "HERE": root}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "generated", "exec"), env)
            rr = rows("А.", "Б.", "В.")
            fx, st = state(rr, 1, part=False)
            st["segs"].pop(nf.row_uids(rr)[0])
            fixture(root, rr, fx, st)
            self.assertEqual(env["fixed_set"](rr), {1})
            put(root, "studio/fixed_parts.json", b"{broken")
            with self.assertRaises(nf.ProtectionError): env["fixed_parts"](rr)

    def test_part_sync_never_rewrites_broken_boundary(self):
        spec = importlib.util.spec_from_file_location("nox_part_test", KIT / "nox_part.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rr = rows("А.", "Б.")
        fx, st = state(rr)
        st["parts"][0]["last"] = "missing.0"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture(root, rr, fx, st)
            before = (root / "studio/fixed_parts.json").read_bytes()
            with self.assertRaises(nf.ProtectionError):
                module.sync_fixed(types.SimpleNamespace(HERE=root, load_rows=lambda: rr))
            self.assertEqual((root / "studio/fixed_parts.json").read_bytes(), before)

    def test_status_and_report_leave_every_book_file_unchanged(self):
        # Real entry point + imported local runtime; bytecode creation is also
        # a filesystem write and must not occur on either diagnostic path.
        runtime = '''from pathlib import Path
import csv, json, re
HERE=Path(__file__).resolve().parent
WORK=HERE/'work'
PARTS=HERE/'parts'
NOX_STAGE=21
NOX_PROTECTION_VERSION='18d'
SETTINGS={}
RE_RU_LETTERS=re.compile('[^А-Яа-яЁё]')
def load_rows():
    with (WORK/'segments.csv').open(encoding='utf-8') as f: return list(csv.DictReader(f,delimiter=';'))
def load_roles(): return json.loads((WORK/'roles.json').read_text())
def work_window(rows, roles): return (0,len(rows)-1,1,False)
def fixed_prefix(rows): return []
def fixed_set(rows, fx=None): return set()
def wav_for(row, roles): return WORK/'wav'/str(row['id'])
def _asr_gate(): return False
def by_parts(): return True
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture(root, rows("А.", "Б."))
            (root / "nox_audiobook.py").write_text(runtime, encoding="utf-8")
            shutil.copyfile(KIT / "nox_freeze.py", root / "nox_freeze.py")
            def snapshot():
                return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            before = snapshot()
            for flag in ("--status", "--report"):
                result = subprocess.run([sys.executable, str(KIT / "nox_part.py"), "--root", str(root), flag],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(snapshot(), before)

    def test_runtime_catalog_uses_exact_mapping_and_retained_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rr = rows("Произнесённый текст.", "Не озвучено.")
            fixture(root, rr)
            wav = put(root, "work/wav/exact_key.wav", b"RIFF" + b"audio" * 20)
            side = put(root, "work/wav/exact_key.txt", b"spoken text with stress")
            runtime = {"wav_for": lambda r, roles: wav if r["text"] == rr[0]["text"] else wav.with_name("missing.wav"),
                       "resolve_role": lambda speaker, roles: "narrator"}
            with nf.preparation(root) as tx:
                nf._record_pipeline_catalog(root, tx, runtime)
                record = tx["manifest"]["pipeline_audio"][0]
                self.assertEqual(len(tx["manifest"]["pipeline_audio"]), 1)
                self.assertEqual(record["uid"], nf.row_uids(rr)[0])
                self.assertEqual(record["text"], rr[0]["text"])
                self.assertEqual(record["role"], "narrator")
                self.assertTrue(tx["manifest"]["catalog_ready"])
            nf.preserve_pipeline_bundle(root, wav)
            wav.unlink()
            side.unlink()
            self.assertEqual(nf._file_hash(root / record["retained_wav"]), record["sha256"])
            self.assertEqual(nf._file_hash(root / record["sidecars"]["txt"]["retained_path"]),
                             record["sidecars"]["txt"]["sha256"])


class WorkerOwnership(unittest.TestCase):
    """Execute actual nested worker functions against a moving-window fixture."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rr = rows("Я.")
        fixture(self.root, self.rr)
        self.wav = self.root / "work/wav/one.wav"
        self.wav.parent.mkdir()
        self.window = [0, 0, 1, False]
        self.claims, self.releases, self.writes = [], [], []
        n = types.SimpleNamespace(HERE=self.root, WORK=self.root / "work", CSV_PATH=self.root / "work/segments.csv",
                                  ROLES_PATH=self.root / "work/roles.json", wav_for=lambda r, roles: self.wav,
                                  work_window=lambda rows, roles: tuple(self.window),
                                  claim=lambda p: self.claims.append(p) or True,
                                  release=lambda p: self.releases.append(p),
                                  resolve_role=lambda speaker, roles: speaker,
                                  prepare_text=lambda text, *args: text,
                                  write_wav_atomic=lambda *args: self.writes.append(args))
        stat = n.CSV_PATH.stat()
        self.env = dict(n=n, rows=self.rr, roles={}, row_indices={id(self.rr[0]): 0}, BP=True,
                        ProtectionError=nf.ProtectionError, read_protection=nf.read_protection,
                        csv_signature=(stat.st_size, stat.st_mtime_ns),
                        protection_cache={"signature": None, "state": None, "audio": set()},
                        state={"model": None}, args=types.SimpleNamespace(accent="acute", max_new_tokens=1500,
                                                                     model="fake", max_seq_len=100, serve=False),
                        RE_LETTERS=re.compile("[^А-Яа-яЁё]"), VOWELS_RE=re.compile("[АЕЁИОУЫЭЮЯаеёиоуыэюя]"),
                        nonstream=False, split_min=10, split_sent=True, tail_sfx="", sent_pause=.3,
                        attempts=1, cut_db=None, vcut=None, vcut_min=0, tail_max=-16,
                        RATE_MAX=100, SPEECH_MIN_S=0, SPEECH_PER_LETTER=0,
                        abrupt_end=lambda *a: False, speech_dur=lambda *a: 1,
                        prompt_for=lambda role: None, prompt_xv=lambda role: None,
                        ref_hz_for=lambda role: None, refhz={}, method="test", tag="[test]",
                        cnt=Counter(), fail_row=Counter(), audio_sec=[0.], done_chars=[0],
                        failed_log=self.root / "failed.txt", susp_log=self.root / "susp.txt",
                        retry_log=self.root / "retry.txt", time=time, os=os, Path=Path)
        tree = ast.parse((KIT / "nox_fast.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        chosen = {"protection_now", "row_is_owned", "synth_row", "get_model", "serve_queue"}
        defs = [n for n in main.body if isinstance(n, ast.FunctionDef) and n.name in chosen]
        exec(compile(ast.Module(body=defs, type_ignores=[]), "nox_fast actual functions", "exec"), self.env)

    def test_window_shrinks_after_claim_without_synth_or_write(self):
        def claim(p):
            self.claims.append(p)
            self.window[1] = -1
            return True
        self.env["n"].claim = claim
        self.env["get_model"] = lambda: self.fail("a stale window must not start the model")
        self.assertEqual(self.env["synth_row"](self.rr[0]), "busy")
        self.assertEqual(len(self.claims), 1)
        self.assertEqual(len(self.releases), 1)
        self.assertEqual(self.writes, [])

    def test_part_closes_during_generation_without_publication(self):
        self.env["get_model"] = lambda: object()
        def synth(*args, **kwargs):
            self.window[3] = True
            return [0.] * 100, 100, 1., False, 0.
        self.env["synth_text"] = synth
        self.assertEqual(self.env["synth_row"](self.rr[0]), "busy")
        self.assertEqual(self.writes, [])
        self.assertFalse(self.wav.with_suffix(".txt").exists())
        self.assertEqual(len(self.releases), 1)

    def test_csv_change_stops_before_claim(self):
        put(self.root, "work/segments.csv", csv_data(rows("Другой текст.")))
        with self.assertRaises(nf.ProtectionError): self.env["synth_row"](self.rr[0])
        self.assertEqual(self.claims, [])

    def test_ready_heartbeat_is_absent_until_successful_warmup(self):
        queue = self.root / "work/studio_queue"
        queue.mkdir()
        alive = queue / "alive_w1.txt"
        self.env.update(QDIR=queue, alive_f=alive, qstat={"beat": 0., "served": 0})
        self.assertEqual(self.env["serve_queue"](), 0)
        self.assertFalse(alive.exists())
        class Bad:
            def warmup(self, **kwargs): raise RuntimeError("warmup failed")
        self.env["load_tts"] = lambda *a: Bad()
        with self.assertRaises(RuntimeError): self.env["get_model"]()
        self.env["serve_queue"]()
        self.assertFalse(alive.exists())
        class Good:
            def warmup(self, **kwargs): pass
        self.env["load_tts"] = lambda *a: Good()
        self.env["get_model"]()
        self.env["serve_queue"]()
        self.assertTrue(alive.exists())


class ProcessLiveness(unittest.TestCase):
    """Mock Win32 read-only calls; these tests are not Windows runtime tests."""
    def win_probe(self, *, handle=0x123456789ABC, exit_code=259, error=0,
                  query_result=1, query_error=None, close_result=1):
        import ctypes
        def query(h, value):
            if query_error is not None:
                raise query_error
            value._obj.value = exit_code
            return query_result
        api = types.SimpleNamespace(OpenProcess=Mock(return_value=handle),
              GetExitCodeProcess=Mock(side_effect=query), CloseHandle=Mock(return_value=close_result),
              TerminateProcess=Mock(side_effect=AssertionError("must never terminate a process")))
        with patch.object(nf.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", return_value=api, create=True) as loader, \
             patch.object(ctypes, "get_last_error", return_value=error, create=True), \
             patch.object(nf.os, "kill", side_effect=AssertionError("Windows must never call os.kill")) as kill:
            result = nf._pid_alive(1234)
        loader.assert_called_once_with("kernel32", use_last_error=True)
        api.OpenProcess.assert_called_once_with(0x1000, False, 1234)
        self.assertEqual(api.OpenProcess.restype, ctypes.c_void_p)
        kill.assert_not_called()
        api.TerminateProcess.assert_not_called()
        if handle:
            api.CloseHandle.assert_called_once_with(handle)
        else:
            api.CloseHandle.assert_not_called()
            api.GetExitCodeProcess.assert_not_called()
        return result

    def test_windows_running_and_exited_process_only_query_information(self):
        self.assertTrue(self.win_probe(exit_code=259))
        self.assertFalse(self.win_probe(exit_code=0))
        self.assertFalse(self.win_probe(exit_code=123))

    def test_windows_missing_pid_is_distinguished_from_access_denied(self):
        self.assertFalse(self.win_probe(handle=None, error=87))
        for error in (5, 0, 6, 1168):
            with self.subTest(error=error):
                self.assertTrue(self.win_probe(handle=None, error=error))

    def test_windows_query_or_close_failure_keeps_recovery_blocked(self):
        self.assertTrue(self.win_probe(query_result=0, exit_code=0))
        self.assertTrue(self.win_probe(query_error=OSError("query denied")))
        self.assertTrue(self.win_probe(exit_code=0, close_result=0))

    def test_windows_unavailable_api_never_falls_back_to_kill(self):
        import ctypes
        with patch.object(nf.os, "name", "nt"), \
             patch.object(ctypes, "WinDLL", side_effect=OSError("API unavailable"), create=True), \
             patch.object(nf.os, "kill", side_effect=AssertionError("unsafe fallback")) as kill:
            self.assertTrue(nf._pid_alive(1234))
        kill.assert_not_called()

    def test_invalid_pid_is_rejected_before_any_native_call(self):
        import ctypes
        for platform in ("nt", "posix"):
            maximum = 0xFFFFFFFF if platform == "nt" else 0x7FFFFFFF
            for pid in (None, "1234", 1.5, True, False, 0, -1, maximum + 1):
                with self.subTest(platform=platform, pid=pid), patch.object(nf.os, "name", platform), \
                     patch.object(ctypes, "WinDLL", create=True) as api, patch.object(nf.os, "kill") as kill:
                    with self.assertRaises(nf.ProtectionError): nf._pid_alive(pid)
                    api.assert_not_called()
                    kill.assert_not_called()

    def test_posix_keeps_signal_zero_probe_with_fail_closed_errors(self):
        for error, expected in ((None, True), (ProcessLookupError(), False), (PermissionError(), True), (OSError(), True)):
            with self.subTest(error=error), patch.object(nf.os, "name", "posix"), \
                 patch.object(nf.os, "kill", side_effect=error) as kill:
                self.assertEqual(nf._pid_alive(1234), expected)
                kill.assert_called_once_with(1234, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
