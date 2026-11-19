"""Legacy whole-part protection with an explicitly selected, individually unlocked take.

All fixtures here are synthetic. No manuscript, author edit or production UID
from the user's diagnostic archive is embedded in this release.
"""
from __future__ import annotations
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import contextlib
from protection_regressions import rows, state, fixture, put, generated_module, FIELDS
import nox_freeze as nf
import nox_voice_safety as vs


class LegacyPartProtection(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.rows=rows('Защищённое начало.', 'Выбранная запись автора.', 'Открытый хвост.')
        self.fx,self.edits=state(self.rows,1)
        self.uid=nf.row_uids(self.rows)[1]
        entry=self.edits['segs'][self.uid]
        entry.pop('locked')
        entry['take']='t003'
        fixture(self.root,self.rows,self.fx,self.edits)
        self.wav=put(self.root,'studio/takes/s1/s1_t003.wav',b'RIFF'+b'selected author audio'*10)
        self.meta=put(self.root,'studio/takes/s1/s1_t003.json',dict(tid='t003',cover=[],role='narrator',tech='Выбранная запись автора.',src='synth'))

    def tearDown(self): self.tmp.cleanup()

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():(p.read_bytes(),p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_legacy_selection_does_not_fabricate_locks_but_protects_complete_range(self):
        before=copy.deepcopy((self.fx,self.edits))
        result=nf.validate_protection(self.rows,self.fx,self.edits)
        self.assertEqual(result.part_selected_uids,{self.uid})
        self.assertNotIn(self.uid,result.locked_uids)
        self.assertEqual(result.protected_indices,{0,1})
        self.assertEqual(result.ranges,((0,1,1),))
        self.assertEqual((self.fx,self.edits),before)
        self.assertNotIn('locked',self.edits['segs'][self.uid])

    def test_missing_selection_or_unproven_join_still_stops(self):
        for problem in ('entry','sid','take','into','join','locked'):
            with self.subTest(problem=problem):
                edits=copy.deepcopy(self.edits)
                if problem=='entry': edits['segs'].pop(self.uid)
                elif problem in ('sid','take'): edits['segs'][self.uid].pop(problem)
                else: edits['segs'][self.uid][problem]={'into':'missing-head','join':['missing-child'],'locked':{}}[problem]
                with self.assertRaises(nf.ProtectionError): nf.validate_protection(self.rows,self.fx,edits)

    def test_legacy_selection_does_not_weaken_boundary_or_json_validation(self):
        variants=[]
        wrong=copy.deepcopy(self.fx);wrong['parts'][0]['b_uid']='missing';variants.append(wrong)
        wrong=copy.deepcopy(self.fx);wrong['parts'].append(copy.deepcopy(wrong['parts'][0]));variants.append(wrong)
        wrong=copy.deepcopy(self.fx);wrong['parts'][0].update(b=0,b_uid=nf.row_uids(self.rows)[0]);variants.append(wrong)
        for wrong in variants:
            with self.assertRaises(nf.ProtectionError): nf.validate_protection(self.rows,wrong,self.edits)
        for relative in ('studio/edits.json','studio/fixed_parts.json'):
            path=self.root/relative;before=path.read_bytes();path.write_bytes(b'{broken')
            with self.assertRaises(nf.ProtectionError): nf.read_protection(self.root,self.rows)
            path.write_bytes(before)

    def test_audio_validation_checks_the_selected_take_and_writes_nothing(self):
        before=self.snapshot()
        result=nf.read_protection(self.root,self.rows,check_audio=True)
        self.assertEqual(result.part_selected_uids,{self.uid})
        self.assertEqual(self.snapshot(),before)
        expected=(self.wav,self.meta)
        self.assertEqual(tuple(nf.part_selected_audio_paths(self.root,self.edits,result)),expected)
        records=nf.part_selected_records(self.root,self.edits,result)
        self.assertEqual(records[0]['take'],'t003')
        self.assertEqual(records[0]['role'],'narrator')
        self.assertEqual(records[0]['sha256'],hashlib.sha256(self.wav.read_bytes()).hexdigest())

    def test_missing_bad_or_mismatched_selected_sidecar_stops(self):
        original=self.meta.read_bytes()
        cases=[None,b'{broken',json.dumps({'tid':'t002'}).encode(),json.dumps({'tid':'t003','cover':['unexpected']}).encode()]
        for data in cases:
            with self.subTest(data=data):
                if data is None:self.meta.unlink(missing_ok=True)
                else:self.meta.write_bytes(data)
                with self.assertRaises(nf.ProtectionError):nf.read_protection(self.root,self.rows,check_audio=True)
                self.meta.write_bytes(original)
        self.wav.unlink()
        with self.assertRaises(nf.ProtectionError):nf.read_protection(self.root,self.rows,check_audio=True)

    def test_another_selected_take_cannot_silently_reuse_previous_bundle(self):
        self.edits['segs'][self.uid]['take']='t004'
        put(self.root,'studio/edits.json',self.edits)
        with self.assertRaisesRegex(nf.ProtectionError,'t004'):nf.read_protection(self.root,self.rows,check_audio=True)
        self.assertTrue(self.wav.exists())
        self.assertEqual(json.loads(self.meta.read_text())['tid'],'t003')

    def test_freeze_snapshots_selection_and_preserves_its_unlocked_state(self):
        kept={p:p.read_bytes() for p in (self.wav,self.meta,self.root/'studio/edits.json',self.root/'studio/fixed_parts.json')}
        with nf.preparation(self.root) as tx:
            record=tx['manifest']['part_selected'][0]
            self.assertEqual((record['uid'],record['sid'],record['take']),(self.uid,'s1','t003'))
            self.assertEqual(record['role'],'narrator')
            self.assertEqual(tx['manifest']['audio'][self.wav.relative_to(self.root).as_posix()],hashlib.sha256(kept[self.wav]).hexdigest())
            self.assertEqual(tx['manifest']['audio'][self.meta.relative_to(self.root).as_posix()],hashlib.sha256(kept[self.meta]).hexdigest())
            out=nf.commit_prepared_rows(self.root,rows('Защищённое начало.','Выбранная запись автора.','Новая концовка.'),FIELDS)
            self.assertEqual(out[1]['text'],self.rows[1]['text'])
            saved=json.loads((tx['snapshot']/'studio/edits.json').read_text())
            self.assertNotIn('locked',saved['segs'][self.uid])
        for p,b in kept.items():self.assertEqual(p.read_bytes(),b)
        self.assertNotIn('locked',json.loads((self.root/'studio/edits.json').read_text())['segs'][self.uid])
        with self.assertRaises(nf.ProtectionError):
            nf.merge_locked_prefix(self.rows,rows('Защищённое начало.','Изменённая выбранная фраза.','Хвост.'),self.fx,self.edits)

    def test_selected_audio_change_during_prepare_cancels_before_csv_write(self):
        csv_before=(self.root/'work/segments.csv').read_bytes()
        for changed in (self.wav,self.meta):
            original=changed.read_bytes()
            with nf.preparation(self.root):
                changed.write_bytes(original+b' ')
                with self.assertRaises(nf.ProtectionError):
                    nf.commit_prepared_rows(self.root,rows('Защищённое начало.','Выбранная запись автора.','Новая концовка.'),FIELDS)
            self.assertEqual((self.root/'work/segments.csv').read_bytes(),csv_before)
            changed.write_bytes(original)

    def test_unlocking_whole_part_removes_only_derived_part_protection(self):
        edits=copy.deepcopy(self.edits)
        edits['parts'][0]['locked']=None
        result=nf.validate_protection(self.rows,{'ver':1,'parts':[]},edits)
        self.assertEqual(result.part_selected_uids,set())
        self.assertEqual(result.protected_indices,{0})
        self.assertEqual(edits['segs'][self.uid]['take'],'t003')

    def test_selected_insert_is_protected_without_fabricating_snapshot(self):
        edits=copy.deepcopy(self.edits)
        iid='ins_synthetic'
        edits['ins']=[dict(id=iid,after=self.uid,ord=1,text='Добавление.',role='narrator')]
        edits['segs'][iid]={'sid':'sins','take':'t007'}
        put(self.root,'studio/takes/sins/sins_t007.wav',b'RIFF'+b'insertion audio'*10)
        put(self.root,'studio/takes/sins/sins_t007.json',dict(tid='t007'))
        put(self.root,'studio/edits.json',edits)
        result=nf.read_protection(self.root,self.rows,check_audio=True)
        self.assertEqual(result.part_selected_uids,{self.uid,iid})
        self.assertNotIn(iid,result.locked_uids)
        self.assertNotIn('locked',edits['segs'][iid])

    def test_voice_maintenance_protects_selected_role_and_bundle(self):
        roles={'roles':{'narrator':{'count':3},'Авторский голос':{'instruct':'approved override'},
                       'Прежний голос':{'instruct':'source sample'}},'aliases':{}}
        put(self.root,'work/roles.json',roles)
        self.edits['segs'][self.uid]['role']='Авторский голос'
        put(self.root,'studio/edits.json',self.edits)
        put(self.root,'studio/takes/s1/s1_t003.json',dict(tid='t003',role='Прежний голос'))
        n=generated_module(self.root)
        for role in roles['roles']:
            for p in n.voice_files(role,n.voice_stems(roles)):put(self.root,p.relative_to(self.root).as_posix(),b'original sample '+p.name.encode())
        wav_before,meta_before=self.wav.read_bytes(),self.meta.read_bytes()
        with vs.RoleSafety(n) as guard:
            self.assertTrue({'narrator','Авторский голос','Прежний голос'}<=guard.protected)
            for p in (self.wav,self.meta):self.assertIn(p.relative_to(self.root).as_posix(),guard.journal['guards'])
            changed=copy.deepcopy(roles)
            changed['roles']['Авторский голос']['instruct']='must not apply'
            changed['roles']['Прежний голос']['instruct']='must not apply'
            with contextlib.redirect_stdout(io.StringIO()):guard.write_roles(changed)
            self.assertEqual(guard.retire_samples({'Авторский голос','Прежний голос'}),0)
        self.assertEqual(n.load_roles(),roles)
        self.assertEqual(self.wav.read_bytes(),wav_before)
        self.assertEqual(self.meta.read_bytes(),meta_before)


if __name__=='__main__':unittest.main(verbosity=2)
