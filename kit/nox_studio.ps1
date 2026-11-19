# nox_studio.ps1 — запуск проверенного полного комплекта «Нокс Студии».
# NOX_STUDIO 2026-11-18d
# Обновление выполняет Update_NOX_18d.ps1. Этот запуск не заменяет код по одному файлу.
# Ключи -Root, -NoStart, -NewBook, -Import сохранены; -Snapshot обновляет снимок версий.
param([string]$Root = "D:\kniga1a", [switch]$NoStart, [switch]$NewBook,
      [string]$Import = "", [string]$NoxHome = "", [string]$Pack = "", [switch]$Snapshot)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"
$Src = if ($NoxHome) { $NoxHome } else { "D:\nox_audiobook" }
$recordFile = Join-Path $Root "work\nox_release.json"
if (-not (Test-Path -LiteralPath $recordFile)) { $recordFile = Join-Path $Src "studio\nox_release.json" }
if (Test-Path -LiteralPath $recordFile) {
    $releaseInfo = Get-Content -LiteralPath $recordFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $NoxHome -and $releaseInfo.home) { $Src = [string]$releaseInfo.home }
    if (-not $Pack) { $Pack = [string]$releaseInfo.kit_dir }
}
if (-not $Pack -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'nox_update.py'))) { $Pack = $PSScriptRoot }
$App = Join-Path $Src 'studio'
$KIT = "NOX_STUDIO 2026-11-18d"
$py = Join-Path $Root 'envfast\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { $py = Join-Path $Src 'envfast\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $py)) { throw 'Нет рабочего envfast; запуск не меняет пакеты Python.' }
if (-not $Pack -or -not (Test-Path -LiteralPath (Join-Path $Pack 'nox_update.py'))) {
    throw 'Нет полного проверяемого комплекта 18d. Сначала примените Update_NOX_18d.ps1 из распакованного архива.'
}
$bundleDir = Split-Path -Parent $Pack
$checkScope = if ($NewBook -or $Import) { 'app' } else { 'all' }
& $py -B (Join-Path $Pack 'nox_update.py') --root $Root --home $Src --bundle $bundleDir --verify-installed $checkScope --output (Join-Path $env:USERPROFILE 'Downloads')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pyw)) { $pyw = $py }
Write-Host '=== Нокс Студия: проверенный комплект 18d' -ForegroundColor Cyan

# ---- загрузка НОВОЙ книги: файл docx/txt/epub/fb2 -> <папка книги>\book.docx + отчёт + действующие лица
if ($NewBook -or $Import) {
    $book = $Import
    if (-not $book) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $dlg = New-Object System.Windows.Forms.OpenFileDialog
            $dlg.Title = "Nox Studio: book file"
            $dlg.Filter = "Books (docx, txt, epub, fb2)|*.docx;*.txt;*.epub;*.fb2;*.fb2.zip;*.fbz|All files|*.*"
            if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $book = $dlg.FileName }
        } catch { Write-Host ("Окно выбора файла не открылось: " + $_.Exception.Message) -ForegroundColor Red }
    }
    if (-not $book -or -not (Test-Path -LiteralPath $book)) { Write-Host "Файл книги не выбран — ничего не сделано." -ForegroundColor Yellow; exit 1 }
    if ((Test-Path "$Root\book.docx") -and (Test-Path "$Root\work\segments.csv")) {
        Write-Host ("В " + $Root + " уже есть подготовленная книга. Для новой книги укажите другую папку: -Root D:\kniga4") -ForegroundColor Red
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Write-Host ("=== Загружаю книгу: " + $book) -ForegroundColor Cyan
    & $py "$App\nox_import.py" $book --out $Root
    if ($LASTEXITCODE -ne 0) { Write-Host "Книга не загружена (причина — строкой выше). Исходный файл не изменён." -ForegroundColor Red; exit 1 }
    if (Test-Path "$Root\import_report.txt") { Start-Process notepad.exe "$Root\import_report.txt" }
    Write-Host ("=== Готово: " + $Root + "\book.docx, import_report.txt, roles_scan.tsv") -ForegroundColor Green
    Write-Host "  Проверьте отчёт (главы, действующие лица, уязвимые места). Следующий шаг — голоса для ролей этой книги:"
    Write-Host "  пришлите Claude файлы import_report.txt и roles_scan.tsv — по ним готовятся файлы голосов и команда озвучки."
    exit 0
}

if (-not (Test-Path "$Root\work\segments.csv")) {
    Write-Host ("Книга в " + $Root + " ещё не подготовлена (нет work\segments.csv). Сначала запустите go.") -ForegroundColor Red
    exit 1
}

# значок и ярлыки (рабочий стол + меню «Пуск»)
if (-not (Test-Path -LiteralPath "$App\nox_studio.ico")) {
    & $py -B "$App\nox_studio.py" --icon "$App\nox_studio.ico"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать значок Studio; см. ошибку выше." }
}
$leaf = Split-Path $Root -Leaf
$lnkName = if ($leaf.ToLower() -eq "kniga1a") { "Nox Studio.lnk" } else { "Nox Studio ($leaf).lnk" }
try {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
        if (-not $dir) { continue }
        $lnk = $ws.CreateShortcut((Join-Path $dir $lnkName))
        $lnk.TargetPath = $pyw
        $lnk.Arguments = '"' + "$App\nox_studio.py" + '" --root "' + $Root + '"'
        $lnk.WorkingDirectory = $Root
        if (Test-Path "$App\nox_studio.ico") { $lnk.IconLocation = "$App\nox_studio.ico" }
        $lnk.Description = "Nox Studio"
        $lnk.Save()
    }
    Write-Host ("  ярлык «" + ($lnkName -replace '\.lnk$', '') + "» — на рабочем столе и в меню «Пуск»")
} catch { Write-Host ("  ярлык не создан (" + $_.Exception.Message + ") — студию можно запускать этой же командой") -ForegroundColor Yellow }

# снимок окружения: по нему чистая установка повторит рабочую один в один
$lock = "$Src\lock"
if ($Snapshot -or -not (Test-Path -LiteralPath "$lock\manifest_18d.json")) {
New-Item -ItemType Directory -Force -Path $lock | Out-Null
$snapPy = @'
import json, os, platform, subprocess, sys, time
from importlib import metadata
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "studio"))
from nox_release_io import atomic_write
from pathlib import Path
src, root, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception as e:
        return "? " + type(e).__name__
man = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "kit": sys.argv[4], "windows": platform.platform(),
       "python_envfast": sys.version.split()[0],
       "gpu": run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]),
       "ffmpeg": [str(p) for p in (src / "tools").rglob("ffmpeg.exe")][:2] if (src / "tools").is_dir() else [],
       "models": [], "packages_key": {}}
hub = src / "hf" / "hub"
if hub.is_dir():
    for m in sorted(hub.glob("models--*")):
        snaps = sorted(p.name for p in (m / "snapshots").iterdir()) if (m / "snapshots").is_dir() else []
        size = sum(f.stat().st_size for f in m.rglob("*") if f.is_file())
        man["models"].append({"name": m.name[8:].replace("--", "/"), "snapshots": snaps, "mb": round(size / 1e6)})
for name, dist in (("torch", "torch"), ("torchaudio", "torchaudio"), ("transformers", "transformers"),
                   ("faster_qwen3_tts", "faster-qwen3-tts"), ("qwen_tts", "qwen-tts"),
                   ("faster_whisper", "faster-whisper"), ("ctranslate2", "ctranslate2"),
                   ("ruaccent", "ruaccent"), ("onnxruntime", "onnxruntime"), ("numpy", "numpy"),
                   ("soundfile", "soundfile"), ("librosa", "librosa"), ("docx", "python-docx")):
    try:
        man["packages_key"][name] = metadata.version(dist)
    except metadata.PackageNotFoundError:
        providers = metadata.packages_distributions().get(name, [])
        man["packages_key"][name] = {d: metadata.version(d) for d in providers} if providers else "не найден дистрибутив"
man["cuda_note"] = "Загрузка torch/модели при снимке не выполнялась; GPU/драйвер — в поле gpu."
(out / "envfast.lock.txt").write_text(run([sys.executable, "-m", "pip", "freeze"]) + "\n", encoding="utf-8")
env_py = root / "env" / "python.exe"
if env_py.exists():
    (out / "env.lock.txt").write_text(run([str(env_py), "-m", "pip", "freeze"]) + "\n", encoding="utf-8")
    man["python_env"] = run([str(env_py), "-c", "import sys; print(sys.version.split()[0])"])
atomic_write(out / "manifest.json", json.dumps(man, ensure_ascii=False, indent=1))
atomic_write(out / "manifest_18d.json", json.dumps(man, ensure_ascii=False, indent=1))
print("  снимок окружения:", out, "(пакетов в envfast:", len((out / "envfast.lock.txt").read_text(encoding="utf-8").splitlines()), ")")
'@
[IO.File]::WriteAllText("$lock\snapshot.py", $snapPy, (New-Object System.Text.UTF8Encoding $false))
$env:HF_HOME = "$Root\hf"
& $py "$lock\snapshot.py" $Src $Root $lock $KIT
if ($LASTEXITCODE -ne 0) { Write-Host "  снимок окружения не снят — студии это не мешает" -ForegroundColor Yellow }
}

if ($NoStart) {
    if ($env:NOX_FROM_GO -eq "1") { Write-Host "=== Редактор установлен. Окно откроется само, когда go соберёт часть (пока идёт озвучка, окна нет — так и должно быть); позже — ярлыком «Nox Studio» на рабочем столе." -ForegroundColor Green }
    else { Write-Host "=== Готово. Запуск — ярлыком «Nox Studio»." -ForegroundColor Green }
    exit 0
}
Write-Host ("=== Запускаю студию для книги " + $Root) -ForegroundColor Cyan
Start-Process -FilePath $pyw -ArgumentList ('"' + "$App\nox_studio.py" + '" --root "' + $Root + '"') -WorkingDirectory $Root
Write-Host "  окно откроется через несколько секунд. Если не открылось — журнал: $Root\studio\studio.log"

