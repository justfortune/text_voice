"""Real maintenance entry points and publication/recovery boundaries."""
from __future__ import annotations
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from protection_regressions import KIT, rows, state, fixture, put, generated_module
import nox_freeze as nf
import nox_voice_safety as vs


def script(name, n, root, argv=None):
    spec = importlib.util.spec_from_file_location("tested_" + name, KIT / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nox_audiobook": n}), patch.object(sys, "argv", argv or [name, "roles", str(root)]):
        spec.loader.exec_module(mod)
    mod.ROOT = root
    if hasattr(mod, "WORK"): mod.WORK = root / "work"
    if hasattr(mod, "XLSX"): mod.XLSX = root / "Настройки озвучки.xlsx"
    if hasattr(mod, "SETTINGS_JSON"): mod.SETTINGS_JSON = root / "work/settings.json"
    return mod


class RealVoiceScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rows = rows("Закреплённая речь.", "Меняемый голос.", "Рассказывает он.")
        for row, who in zip(self.rows, ("Тина", "капитан", "narrator")):
            row.update(speaker=who, kind="speech" if who != "narrator" else "narr")
        fx, ed = state(self.rows)
        ed["segs"][nf.row_uids(self.rows)[0]]["locked"]["role"] = "Тина"
        fixture(self.root, self.rows, fx, ed)
        self.roles = {"roles": {"narrator": {"instruct": "Narrator", "count": 1},
                               "Тина": {"instruct": "Female voice, old approved voice.", "gender": "жен", "count": 1},
                               "капитан": {"instruct": "Male voice, old captain.", "gender": "муж", "count": 1}}, "aliases": {}}
        put(self.root, "work/roles.json", self.roles)
        self.n = generated_module(self.root)
        self.pairs = {}
        for role in self.roles["roles"]:
            pair = self.n.voice_files(role, self.n.voice_stems(self.roles))
            for p in pair: put(self.root, str(p.relative_to(self.root)), ("original " + p.name).encode())
            self.pairs[role] = pair
        self.wavs = {}
        for row in self.rows:
            p = self.n.wav_for(row, self.roles)
            put(self.root, str(p.relative_to(self.root)), ("RIFF" + row["text"] * 20).encode())
            put(self.root, str(p.with_suffix(".txt").relative_to(self.root)), row["text"].encode())
            self.wavs[row["speaker"]] = p
        self.assets = [self.n.ROLES_PATH] + [p for pair in self.pairs.values() for p in pair] + [p for w in self.wavs.values() for p in (w,w.with_suffix('.txt'))]
        self.before = {p: p.read_bytes() for p in self.assets}

    def tearDown(self): self.tmp.cleanup()

    def protected_unchanged(self):
        self.assertEqual(self.n.load_roles()["roles"]["Тина"], self.roles["roles"]["Тина"])
        for p in self.pairs["Тина"] + (self.wavs["Тина"], self.wavs["Тина"].with_suffix('.txt')):
            self.assertEqual(p.read_bytes(), self.before[p])
        self.assertFalse((self.root / 'work/.nox_roles.lock').exists())

    def archived_unlocked(self):
        for p in self.pairs["капитан"] + (self.wavs["капитан"],):
            self.assertFalse(p.exists())
            self.assertTrue(any(q.read_bytes() == self.before[p] for q in (self.root / 'work').rglob('*')
                                if q.is_file() and q.name == p.name))

    def test_actual_cast_keeps_fixed_role_but_recasts_requested_unlocked(self):
        mod = script('voices_kniga1a', self.n, self.root)
        put(self.root, 'work/recast.txt', 'Тина\nкапитан\n'.encode())
        with contextlib.redirect_stdout(io.StringIO()): mod.main()
        self.protected_unchanged()
        self.archived_unlocked()

    def test_locked_alias_and_same_as_target_survive_cast_pruning(self):
        roles = copy.deepcopy(self.roles)
        roles['aliases']['Тина'] = 'Сохранённый псевдоним'
        roles['roles']['Сохранённый псевдоним'] = {'same_as': 'Прежний источник', 'instruct': 'Alias'}
        roles['roles']['Прежний источник'] = {'instruct': 'Approved original voice'}
        put(self.root, 'work/roles.json', roles)
        mod = script('voices_kniga1a', self.n, self.root)
        with contextlib.redirect_stdout(io.StringIO()): mod.main()
        result = self.n.load_roles()
        for who in ('Тина', 'Сохранённый псевдоним', 'Прежний источник'):
            self.assertEqual(result['roles'][who], roles['roles'][who])
        self.assertEqual(result['aliases']['Тина'], 'Сохранённый псевдоним')

    def test_actual_excel_roles_ignores_frozen_changes_and_applies_unlocked(self):
        mod = script('nastroyki_ozvuchki', self.n, self.root)
        xl = {role: mod.seed_row(role, cfg, None) for role, cfg in self.roles['roles'].items()}
        for who in ('Тина', 'капитан'): xl[who].update(desc='New voice description', recast=True, tempo=145)
        table = []
        with patch.dict(sys.modules, {'nox_audiobook': self.n}), patch.object(mod, 'load_book_voices', return_value=None), \
             patch.object(mod, 'read_roles_from_xlsx', return_value=xl), \
             patch.object(mod, 'write_xlsx', side_effect=lambda **kw: table.extend(kw['role_rows'])), \
             contextlib.redirect_stdout(io.StringIO()):
            mod.cmd_roles()
        self.protected_unchanged()
        self.archived_unlocked()
        self.assertAlmostEqual(self.n.load_roles()['roles']['капитан']['tempo'], 1.45)
        self.assertEqual(next(r for r in table if r['role']=='Тина')['tempo'],100)

    def test_actual_voicecheck_force_never_replaces_locked_voice(self):
        mod = script('voicecheck_k1a', self.n, self.root)
        with patch.object(sys, 'argv', ['voicecheck','--force','Тина','капитан']), \
             patch.object(mod, 'median_f0', return_value=300.), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.protected_unchanged()
        self.archived_unlocked()

    def test_voicecheck_dry_does_not_write_reports_or_assets(self):
        mod = script('voicecheck_k1a', self.n, self.root)
        before = {str(p.relative_to(self.root)): (p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        with patch.object(sys, 'argv', ['voicecheck','--dry','--force','Тина','капитан']), \
             patch.object(mod, 'median_f0', return_value=300.), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual({str(p.relative_to(self.root)): (p.read_bytes(),p.stat().st_mtime_ns)
                          for p in self.root.rglob('*') if p.is_file()}, before)

    def test_operation_failure_restores_roles_samples_and_pipeline_bundle(self):
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with vs.RoleSafety(self.n) as guard:
                changed = copy.deepcopy(self.roles)
                changed['roles']['капитан']['instruct']='new'
                guard.write_roles(changed)
                guard.invalidate_wavs({'капитан'}, changed)
                guard.retire_samples({'капитан'})
                raise RuntimeError('injected failure')
        for p,b in self.before.items(): self.assertEqual(p.read_bytes(), b)
        self.assertFalse((self.root / 'work/.nox_roles.lock').exists())

    def test_explicit_recovery_after_interrupted_voice_operation(self):
        guard = vs.RoleSafety(self.n).__enter__()
        changed = copy.deepcopy(self.roles)
        changed['roles']['капитан']['instruct']='new'
        guard.write_roles(changed)
        guard.invalidate_wavs({'капитан'}, changed)
        guard.retire_samples({'капитан'})
        guard.context.__exit__(None,None,None) # simulate a dead process releasing its OS lock, leaving journal
        guard.context = None
        with self.assertRaisesRegex(nf.ProtectionError,'процесс смены голосов'):
            vs.recover_roles(self.root)
        # An independent edit must stop recovery before any recorded file is restored.
        target = self.pairs['капитан'][1]
        target.write_bytes(b'unknown newer text')
        with patch.object(vs,'_pid_alive',return_value=False), self.assertRaisesRegex(nf.ProtectionError,'вне операции'):
            vs.recover_roles(self.root)
        self.assertEqual(self.n.load_roles()['roles']['капитан']['instruct'],'new')
        target.unlink()
        with patch.object(vs,'_pid_alive',return_value=False): vs.recover_roles(self.root)
        for p,b in self.before.items(): self.assertEqual(p.read_bytes(), b)
        self.assertFalse((self.root / 'work/.nox_roles.lock').exists())


class Publication(unittest.TestCase):
    def test_real_builder_skips_nonprefix_fixed_and_rechecks_before_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rr = rows('А.', 'Б.')
            _, ed = state(rr,1)
            uid = nf.row_uids(rr)
            ed['segs'].pop(uid[0])
            ed['parts'] = [dict(n=1,last=uid[0],locked=None),dict(n=2,last=uid[1],locked='fixed')]
            fixture(root,rr,nf.fixed_document(rr,ed),ed)
            n = generated_module(root)
            old1=put(root,'parts/part_01.mp3',b'old1')
            old2=put(root,'parts/part_02.mp3',b'old2')
            times=put(root,'parts/part_01.times.tsv',b'old times')
            n.plan_parts=lambda *a,**kw: ([(0,0,True),(1,1,True)],2,True)
            n._part_sig=lambda *a,**kw:'new'
            n.assemble=lambda *a,**kw:(b'audio',24000,[],False)
            n._part_check=lambda *a,**kw:'ok'
            n._TL[:]=[(0.,1.,rr[0])]
            called=[]
            def encode(audio,sr,chapters,out,*a,**kw):
                called.append(out.name)
                out.write_bytes(b'new encoded')
                return 1.
            n.encode_m4b=encode
            with contextlib.redirect_stdout(io.StringIO()): n.build_parts(rr,n.load_roles(),19,'Книга','Автор','96k',fmt='mp3')
            self.assertEqual(old1.read_bytes(),b'new encoded')
            self.assertEqual(old2.read_bytes(),b'old2')
            self.assertEqual(len(called),1)
            old1.write_bytes(b'unchanged old1')
            times.write_bytes(b'unchanged times')
            put(root,'work/parts.json',[])
            def lock_during_encode(audio,sr,chapters,out,*a,**kw):
                out.write_bytes(b'must not publish')
                allfixed, full = state(rr,0)
                full['segs'].update(ed['segs'])
                full['parts'] = [dict(n=1,last=uid[0],locked='fixed'),ed['parts'][1]]
                put(root,'studio/edits.json',full)
                put(root,'studio/fixed_parts.json',nf.fixed_document(rr,full))
                return 1.
            n.encode_m4b=lock_during_encode
            with contextlib.redirect_stdout(io.StringIO()): n.build_parts(rr,n.load_roles(),19,'Книга','Автор','96k',fmt='mp3')
            self.assertEqual(old1.read_bytes(),b'unchanged old1')
            self.assertEqual(times.read_bytes(),b'unchanged times')
            self.assertEqual(old2.read_bytes(),b'old2')
            self.assertFalse(list((root/'parts').glob('.nox_part_*')))


if __name__=='__main__': unittest.main(verbosity=2)
