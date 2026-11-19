"""End-to-end checked update of a real pinned-main tree, then byte-exact rollback.

This test does not substitute hashes or bypass compatibility checks. Supply the
actual downloaded main tree and the actual legacy generated runtime:

  python tests/package_upgrade.py --package release.zip \
    --baseline-dir /path/to/main --legacy-runtime /path/to/old/nox_audiobook.py

Equivalent environment variables: NOX_RELEASE_ZIP, NOX_BASELINE_DIR,
NOX_LEGACY_RUNTIME. Without the two historical inputs the test honestly SKIPs.
The Windows-only command-line gate and process discovery are not exercised.
"""
from __future__ import annotations
import argparse
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import unittest
import wave
import zipfile

OPTIONS = None
DETAILS = []


def sha(data): return hashlib.sha256(data).hexdigest()


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def write(root, relative, data):
    p = root / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8'))
    return p


def pcm_wav(value):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(int(value).to_bytes(2,'little',signed=True) * 9600)
    return stream.getvalue()


def book_fixture(book):
    rr = [dict(id=1, chapter=1, ch_title='Глава', para=0, kind='narr', speaker='narrator', conf='high', pause_before_ms=400, text='Утверждённая первая фраза.'),
          dict(id=2, chapter=1, ch_title='Глава', para=1, kind='narr', speaker='narrator', conf='high', pause_before_ms=400, text='Следующая фраза книги.')]
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rr[0]), delimiter=';')
    writer.writeheader(); writer.writerows(rr)
    write(book,'work/segments.csv',stream.getvalue().encode('utf-8'))
    uids = [hashlib.sha1(r['text'].encode()).hexdigest()[:12]+'.0' for r in rr]
    roles = {'roles': {'narrator': {'instruct': 'Approved narrator.', 'count': 2, 'same_as': None}},
             'aliases': {}, 'unknown_role': 'narrator'}
    write(book,'work/roles.json',roles)
    sample, sample_text = pcm_wav(500), 'Проверенный образец рассказчика.'.encode()
    write(book,'work/voices/narrator.wav',sample)
    write(book,'work/voices/narrator.txt',sample_text)
    sig = hashlib.sha1(sample + sample_text).hexdigest()[:8]
    for i,row in enumerate(rr):
        key = hashlib.sha1(('narrator|'+sig+'\n'+row['text']).encode()).hexdigest()[:16]
        write(book,'work/wav/'+key+'.wav',pcm_wav(600+i))
        write(book,'work/wav/'+key+'.txt',row['text'].encode())
    lock = dict(at='2026-11-19 10:00:00',take='t001',role='narrator',tech=rr[0]['text'],tempo=1.0,pause=400,after=[])
    edits = {'v':1,'segs':{uids[0]:dict(sid='sfixture1',take='t001',locked=lock)},'ins':[], 'rules':[], 'cues':[],
             'parts':[dict(n=1,last=uids[0],last_i=0,last_p=0,eq=None,opened='2026-11-19',locked='2026-11-19 10:00:00',export=None)]}
    write(book,'studio/edits.json',edits)
    write(book,'studio/fixed_parts.json',dict(ver=1,parts=[dict(n=1,a=0,b=0,a_uid=uids[0],b_uid=uids[0])],at='2026-11-19'))
    write(book,'studio/sid_map.json',{uids[0]:'sfixture1'})
    write(book,'studio/takes/sfixture1/sfixture1_t001.wav',pcm_wav(700))
    write(book,'studio/takes/sfixture1/sfixture1_t001.json',dict(tid='t001',cover=[],role='narrator',text=rr[0]['text']))
    write(book,'studio/sounds/index.json',dict(v=1,items=[]))
    write(book,'studio/sounds/approved.wav',pcm_wav(800))
    write(book,'work/parts.json',[dict(n=1,a_id=1,b_id=1,sig='fixed',fixed=True)])
    write(book,'work/settings.json',dict(tempo_pct=112,pause_pct=85))
    write(book,'work/run_mode.json',dict(by_parts=True,part_sec=1140))
    write(book,'work/accent_dict.csv','слово;ударение\n'.encode())
    write(book,'parts/part_01.mp3',b'ID3'+b'approved exported bytes'*50)
    write(book,'parts_zvuk/version_1/part_01.mp3',b'ID3'+b'approved mix bytes'*50)
    write(book,'envfast/pyvenv.cfg',b'home = D:\\nox_audiobook\\env\n')
    write(book,'models/not_a_real_model.bin',b'model sentinel must stay untouched')
    doc = io.BytesIO()
    with zipfile.ZipFile(doc,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Книга остаётся прежней.</w:t></w:r></w:p></w:body></w:document>')
    write(book,'book.docx',doc.getvalue())


@contextlib.contextmanager
def from_package(bundle):
    # Import the package being tested, even when another test already imported
    # modules from the candidate working tree in this interpreter.
    old_modules = {k:v for k,v in sys.modules.items() if k.startswith('nox_')}
    for k in old_modules: del sys.modules[k]
    old_path, old_bytecode = list(sys.path), sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0,str(bundle/'kit'))
    try:
        yield importlib.import_module('nox_update')
    finally:
        for k in list(sys.modules):
            if k.startswith('nox_'): del sys.modules[k]
        sys.modules.update(old_modules)
        sys.path[:]=old_path
        sys.dont_write_bytecode=old_bytecode


class PackageUpgrade(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = OPTIONS or argparse.Namespace(package=os.environ.get('NOX_RELEASE_ZIP'),
            baseline_dir=os.environ.get('NOX_BASELINE_DIR'),legacy_runtime=os.environ.get('NOX_LEGACY_RUNTIME'))
        if not options.baseline_dir or not options.legacy_runtime:
            raise unittest.SkipTest('Нужны настоящие NOX_BASELINE_DIR и NOX_LEGACY_RUNTIME; подмена старого кода/хешей не выполняется')
        cls.baseline = Path(options.baseline_dir).resolve()
        cls.legacy = Path(options.legacy_runtime).resolve()
        cls.package = Path(options.package).resolve() if options.package else Path(__file__).resolve().parents[1]
        if not (cls.baseline/'base/nox_audiobook.py').is_file() or not cls.legacy.is_file():
            raise AssertionError('Указанные исторические файлы отсутствуют; это ошибка входных параметров')
        # A supported later package may contain files that did not exist in
        # pinned main. Verify the complete original tree explicitly instead of
        # mistaking every known_previous hash for a file in that old tree.
        baseline_spec = json.loads(Path(__file__).with_name('main_baseline.json').read_text(encoding='utf-8'))
        if baseline_spec['commit'] != 'da86923760db9e90292de175d5fc5ddffe7e2129':
            raise AssertionError('Неожиданная базовая ревизия теста')
        for item in baseline_spec['files']:
            data = (cls.baseline / item['path']).read_bytes()
            git_sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if git_sha != item['git_blob_sha1'] or len(data) != item['bytes']:
                raise AssertionError('Исходник не совпал с pinned main: ' + item['path'])
        if not cls.package.exists(): raise AssertionError('Нет указанного ZIP/распакованного комплекта')
        DETAILS.extend(['Baseline: '+str(cls.baseline),'Legacy runtime SHA256: '+sha(cls.legacy.read_bytes()),
                        'Package: '+str(cls.package),'Package ZIP SHA256: '+sha(cls.package.read_bytes()) if cls.package.is_file() else 'Package: directory'])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.bundle=self.root/'bundle'
        if self.package.is_file():
            with zipfile.ZipFile(self.package) as z:
                self.assertIsNone(z.testzip())
                for item in z.infolist():
                    p=PurePosixPath(item.filename)
                    self.assertFalse(p.is_absolute() or '..' in p.parts or '\\' in item.filename)
                z.extractall(self.bundle)
        else:
            shutil.copytree(self.package,self.bundle,ignore=shutil.ignore_patterns('.git','__pycache__'))
        self.book,self.home,self.downloads=(self.root/p for p in ('book','home','Downloads'))
        for p in (self.book,self.home,self.downloads): p.mkdir()
        book_fixture(self.book)
        self.api_context=from_package(self.bundle)
        self.api=self.api_context.__enter__()
        self.manifest=self.api.read_manifest(self.bundle)
        roots={'book':self.book,'app':self.home/'studio','home':self.home,'downloads':self.downloads}
        self.seeded=[]
        for item in self.manifest['targets']:
            original = self.legacy if item['source']=='runtime/nox_audiobook.py' else self.baseline/item['source']
            if original.is_file():
                target=roots[item['scope']]/item['path']
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(original,target)
                self.seeded.append((item['scope'],item['path']))
            # New components remain absent in the original installation even
            # when the public release also accepts their recovery2 revision.
        self.target_paths={str(roots[t['scope']]/t['path']) for t in self.manifest['targets']}
        self.old_targets={p:Path(p).read_bytes() if Path(p).is_file() else None for p in self.target_paths}
        self.book_before=snapshot(self.book)

    def tearDown(self):
        self.api_context.__exit__(None,None,None)
        self.tmp.cleanup()

    def plan_project(self):
        plan=self.api.make_plan(self.bundle,self.manifest,self.book,self.home,self.downloads)
        project=self.api.check_project(self.book)
        self.assertEqual(project['rows'],2)
        self.assertEqual(project['fixed_parts'],1)
        self.assertEqual(project['protected_rows'],1)
        return plan,project

    def assert_data_unchanged(self):
        for relative,data in self.book_before.items():
            if str(self.book/relative) not in self.target_paths:
                self.assertEqual((self.book/relative).read_bytes(),data,relative)
        allowed_added={Path(p).relative_to(self.book).as_posix() for p in self.target_paths if Path(p).is_relative_to(self.book)}
        added=set(snapshot(self.book))-set(self.book_before)
        self.assertFalse(added-allowed_added-{'work/nox_release.json'},str(added))

    def test_real_main_upgrade_verify_and_byte_exact_rollback(self):
        plan,project=self.plan_project()
        self.assertTrue(all(p['compatible'] for p in plan),str([p['target'] for p in plan if not p['compatible']]))
        self.assertEqual(set(p['scope'] for p in plan),{'book','app','home','downloads'})
        journal=self.api.install(self.bundle,self.manifest,self.book,self.home,plan,project)
        self.assertEqual(json.loads(journal.read_text())['state'],'committed')
        self.api.verify_installed(self.bundle,self.manifest,self.book,self.home,'all')
        for item in plan: self.assertEqual(sha(Path(item['target']).read_bytes()),item['new_hash'])
        self.assert_data_unchanged()
        self.assertEqual(self.api.state_hashes(self.book),project['state_hashes'])
        self.api.restore_code(journal,expected_book=self.book,expected_home=self.home,expected_downloads=self.downloads)
        self.assertEqual(json.loads(journal.read_text())['state'],'rolled_back')
        for p,old in self.old_targets.items():
            self.assertEqual(Path(p).read_bytes() if Path(p).is_file() else None,old,p)
        self.assertEqual(snapshot(self.book),self.book_before)
        DETAILS.append('PASS: actual-main upgrade, '+str(len(self.seeded))+' old destinations / '+str(len(plan))+' planned destinations; all four scopes; verify_installed(all); book/audio byte-exact; rollback exact original code+metadata.')

    def test_unknown_old_code_stops_and_diagnostic_zip_excludes_audio(self):
        unknown=self.book/'nox_fast.py'
        unknown.write_bytes(unknown.read_bytes()+b'\n# unrecognised local modification\n')
        plan,project=self.plan_project()
        bad=[p for p in plan if not p['compatible']]
        self.assertEqual([p['target'] for p in bad],[str(unknown)])
        before={scope:snapshot(root) for scope,root in [('book',self.book),('home',self.home),('downloads',self.downloads)]}
        with self.assertRaisesRegex(self.api.UpdateError,'неизвестные локальные файлы'):
            self.api.install(self.bundle,self.manifest,self.book,self.home,plan,project)
        for scope,root in [('book',self.book),('home',self.home),('downloads',self.downloads)]: self.assertEqual(snapshot(root),before[scope])
        output=self.api.save_diagnostics(self.root/'diagnostics',self.book,self.home,plan,['unknown local nox_fast.py'],project)
        with zipfile.ZipFile(output) as z:
            self.assertIsNone(z.testzip())
            names=z.namelist()
            self.assertIn('book/nox_fast.py',names)
            self.assertIn('book/work/segments.csv',names)
            self.assertIn('book/studio/edits.json',names)
            self.assertIn('book/studio/fixed_parts.json',names)
            self.assertEqual(z.read('book/nox_fast.py'),unknown.read_bytes())
            self.assertFalse([p for p in names if Path(p).suffix.lower() in {'.wav','.mp3','.m4b','.flac','.ogg'}])
            self.assertFalse([p for p in names if '/takes/' in p or '/models/' in p or '/envfast/' in p])
        DETAILS.append('PASS: unknown actual-main source rejected before replacement; diagnostics includes source+CSV+locks, excludes all audio/takes/models/environments ('+str(len(names))+' entries).')


def main():
    global OPTIONS
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',default=os.environ.get('NOX_RELEASE_ZIP'))
    p.add_argument('--baseline-dir',default=os.environ.get('NOX_BASELINE_DIR'))
    p.add_argument('--legacy-runtime',default=os.environ.get('NOX_LEGACY_RUNTIME'))
    p.add_argument('--report',type=Path)
    OPTIONS=p.parse_args()
    capture=io.StringIO()
    result=unittest.TextTestRunner(stream=capture,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PackageUpgrade))
    protocol=['Сквозная проверка ZIP NOX 18d','UTC: '+datetime.now(timezone.utc).isoformat(),*DETAILS,'',capture.getvalue(),
      'Пределы: реальные API из проверяемого ZIP, настоящий pinned main и старый сгенерированный runtime.',
      'Данные книги — отдельная синтетическая двухстрочная фикстура по реальной схеме main; PCM WAV валидны.',
      'Ни CUDA/TTS, ни Windows CLI gate, CIM/процессы Windows или фактическая пользовательская книга здесь не проверяются.',
      'Тест не изменяет хеши known_previous и не подменяет поддержку старой версии. Без исторических входных файлов — SKIP.']
    text='\n'.join(protocol)
    print(text)
    if OPTIONS.report:
        OPTIONS.report.parent.mkdir(parents=True,exist_ok=True)
        OPTIONS.report.write_text(text,encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__': raise SystemExit(main())
