"""Reproduce the release's isolated checks without running TTS or editing a book."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--report', type=Path)
    args = ap.parse_args()
    root = args.root.resolve()
    sections = []
    results = []
    compiled = 0
    for folder in ('kit', 'tools', 'base', 'runtime', 'tests'):
        for file in sorted((root / folder).glob('*.py')):
            ast.parse(file.read_text(encoding='utf-8-sig'), filename=file.relative_to(root).as_posix())
            compiled += 1
    results.append({'group': 'Python syntax', 'passed': True, 'files': compiled})
    # The distributed generated runtime must be reproducible from the shipped base and prep.
    ps = (root / 'kit/prep_kniga1a.ps1').read_text(encoding='utf-8-sig')
    body = ps.split("$patchPy = @'", 1)[1].split("\n'@", 1)[0].lstrip('\n')
    with tempfile.TemporaryDirectory() as td:
        directory = Path(td)
        patch = directory / 'patch.py'
        patch.write_text(body, encoding='utf-8')
        runtime = directory / 'nox_audiobook.py'
        shutil.copyfile(root / 'base/nox_audiobook.py', runtime)
        run = subprocess.run([sys.executable, '-B', str(patch), str(runtime)], capture_output=True, text=True)
        if run.returncode:
            raise RuntimeError(run.stdout + run.stderr)
        a, b = runtime.read_bytes(), (root / 'runtime/nox_audiobook.py').read_bytes()
        if a != b: raise RuntimeError('runtime/nox_audiobook.py differs from base + prep output')
        results.append({'group': 'Generated runtime reproduction', 'passed': True, 'sha256': hashlib.sha256(a).hexdigest()})
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    for pattern in ('backend_*.py', 'protection_*.py', 'installer_*.py', 'maintenance_*.py', 'publishing_*.py'):
        proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', pattern, '-v'],
                              cwd=root, env=environment, capture_output=True, text=True)
        output = (proc.stdout + proc.stderr).replace(str(root), '<комплект>')
        match = re.search(r'Ran (\d+) tests?', output)
        count = int(match.group(1)) if match else 0
        success = proc.returncode == 0 and count > 0
        results.append({'group': pattern, 'passed': success, 'tests': count, 'exit_code': proc.returncode})
        sections.append(pattern + '\n' + output)
        print(pattern, count, 'PASS' if success else 'FAIL', flush=True)
    node = shutil.which('node')
    if node:
        proc = subprocess.run([node, 'tests/frontend_contracts.cjs'], cwd=root, env=environment,
                              capture_output=True, text=True)
        output = proc.stdout + proc.stderr
        count = len(re.findall(r'^PASS\s', output, re.M))
        results.append({'group': 'Actual frontend JS / DOM / HTTP contracts', 'passed': proc.returncode == 0 and count > 0,
                        'tests': count, 'exit_code': proc.returncode})
        sections.append('FRONTEND\n' + output)
        print('frontend', count, 'PASS' if proc.returncode == 0 else 'FAIL', flush=True)
    else:
        results.append({'group': 'Frontend', 'passed': False, 'reason': 'Node.js is required for this development test'})
    total = sum(x.get('tests', 0) for x in results)
    success = all(x['passed'] for x in results)
    header = ('NOX 2026-11-18d / 2026-11-19-recovery2-public1\n'
              'Проверка исходников и изолированных временных проектов.\n'
              f'Всего функциональных проверок: {total}; результат: {"PASS" if success else "FAIL"}.\n'
              'Синтаксические проверки и воспроизводимость runtime в это число не прибавлены.\n'
              'Модели, GPU, реальная книга пользователя и Windows PowerShell 5.1 здесь не запускались.\n'
              'Chromium в этой среде не смог стартовать из-за запрета socket() (EPERM); браузерный тест не засчитан.\n'
              'Для экспериментов с моделями, реальным звуком и целевым Windows нужна отдельная приёмка.\n\n')
    report = header + json.dumps(results, ensure_ascii=False, indent=2) + '\n\n' + '\n'.join(sections)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding='utf-8')
    print(json.dumps({'passed': success, 'tests': total, 'checks': results}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
