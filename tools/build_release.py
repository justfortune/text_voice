"""Create the checked NOX 18d code bundle. No book, environment or network access.

The historical baseline is optional for a new engineering build; supported old
hashes must be supplied from the independently verified main snapshot. Existing
known hashes are retained when rebuilding a distributed bundle.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

VERSION = '2026-11-18d'
BUILD = '2026-11-19-recovery2-public1'
BASE_COMMIT = 'da86923760db9e90292de175d5fc5ddffe7e2129'


def normhash(path):
    return hashlib.sha256(path.read_bytes().removeprefix(b'\xef\xbb\xbf').replace(b'\r\n', b'\n')).hexdigest()


def sources(root):
    top = ['README.md', 'CHANGELOG.md', '.gitignore', '.gitattributes', 'Update_NOX_18d.ps1']
    out = [root / name for name in top if (root / name).is_file()]
    allowed = {'.py', '.ps1', '.html', '.cjs', '.md', '.txt', '.json', '.csv'}
    for folder in ('kit', 'tools', 'base', 'data', 'runtime', 'tests', 'docs'):
        for p in sorted((root / folder).glob('*')):
            if p.is_file() and p.suffix in allowed:
                out.append(p)
    return sorted(set(out))


def make_manifest(root, baseline=None, legacy_runtime=None):
    old_manifest = root / 'release_manifest.json'
    old_targets = {}
    if old_manifest.exists():
        old = json.loads(old_manifest.read_text(encoding='utf-8'))
        old_targets = {(x['scope'], x['path']): x.get('known_previous', []) for x in old['targets']}
    files = [{'source': p.relative_to(root).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
              'bytes': p.stat().st_size} for p in sources(root)]
    names = {f['source'] for f in files}
    targets = []
    verified = json.loads((root / 'tools/verified_legacy_sources.json').read_text(encoding='utf-8'))
    if verified.get('format') != 1:
        raise RuntimeError('Unknown verified legacy source registry format')
    known_legacy = {}
    for item in verified.get('sources', []):
        key = (item['scope'], item['path'])
        if key in known_legacy or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise RuntimeError('Invalid or duplicated verified legacy source')
        known_legacy[key] = item['sha256']

    def add(source, scope, path, previous=None, existing_only=False):
        if source not in names:
            raise RuntimeError('Missing release component: ' + source)
        known = set(old_targets.get((scope, path), []))
        if (scope, path) in known_legacy:
            known.add(known_legacy[(scope, path)])
        if baseline:
            p = baseline / (previous or source)
            if p.is_file(): known.add(normhash(p))
        entry = {'source': source, 'scope': scope, 'path': path, 'known_previous': sorted(known)}
        if existing_only: entry['existing_only'] = True
        targets.append(entry)
        return entry

    app = {'nox_studio.py', 'nox_studio.html', 'nox_studio.ps1', 'nox_cues.py', 'nox_import.py',
           'nox_studio_18d.py', 'nox_sound_catalog.py', 'nox_release_io.py', 'nox_file_transactions.py',
           'nox_freeze.py', 'nox_audio_archive.py', 'nox_update.py'}
    app_only = {'nox_studio.py', 'nox_studio.html', 'nox_cues.py', 'nox_import.py', 'nox_studio_18d.py'}
    for name in sorted(app):
        add('kit/' + name, 'app', name)
    for source in sorted(n for n in names if n.startswith('kit/') and (n.endswith('.py') or n.endswith('.ps1'))):
        name = Path(source).name
        if name not in app_only:
            add(source, 'book', name)
        if name.endswith('.ps1'):
            add(source, 'downloads', name, existing_only=True)
    runtime = add('runtime/nox_audiobook.py', 'book', 'nox_audiobook.py', 'base/nox_audiobook.py')
    if legacy_runtime and legacy_runtime.is_file():
        runtime['known_previous'] = sorted(set(runtime['known_previous']) | {normhash(legacy_runtime)})
    add('base/nox_audiobook.py', 'home', 'nox_audiobook.py')
    add('kit/nox_fast.py', 'home', 'nox_fast.py', existing_only=True)
    for name in ('sborshchik_k1a.ps1', 'v_github.ps1'):
        add('tools/' + name, 'downloads', name, existing_only=True)
    if set(known_legacy) - {(x['scope'], x['path']) for x in targets}:
        raise RuntimeError('Verified legacy source has no exact target')
    return {'format': 1, 'product_version': VERSION, 'build_id': BUILD, 'base_commit': BASE_COMMIT,
            'provenance': 'Rebuilt integration of audited main and archived 18c/18d intent; not the missing historical ZIP.',
            'files': files, 'targets': targets}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--legacy-runtime', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    root = args.root.resolve()
    manifest = make_manifest(root, args.baseline, args.legacy_runtime)
    mp = root / 'release_manifest.json'
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.output.mkdir(parents=True, exist_ok=True)
    out = args.output / 'NOX_18d_recovery2_public1_2026-11-19.zip'
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for entry in manifest['files']:
            z.write(root / entry['source'], entry['source'])
        z.write(mp, 'release_manifest.json')
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None: raise RuntimeError('ZIP CRC failure')
        if len(z.namelist()) != len(set(z.namelist())): raise RuntimeError('Duplicate ZIP entries')
        for entry in manifest['files']:
            if hashlib.sha256(z.read(entry['source'])).hexdigest() != entry['sha256']:
                raise RuntimeError('ZIP content differs: ' + entry['source'])
    result = {'archive': str(out), 'files': len(manifest['files']) + 1, 'bytes': out.stat().st_size,
              'sha256': hashlib.sha256(out.read_bytes()).hexdigest(), 'build': BUILD}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
