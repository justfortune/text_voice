# nox_studio.ps1 — установка/обновление и запуск «Нокс Студии» (ручная правка аудиокниги поверх черновой озвучки).
# NOX_STUDIO 2026-11-11g
#
# Что делает:
#   1) кладёт nox_studio.py, nox_studio.html, nox_cues.py (распознаватель мест для звуков) и nox_import.py (загрузка
#      книг docx/txt/epub/fb2) из Загрузок в D:\nox_audiobook\studio (общая часть, как окружения и модели);
#   2) делает ярлык «Nox Studio» на рабочем столе и в меню «Пуск» (со своим значком) — дальше запускать ярлыком;
#   3) снимает «снимок окружения» в D:\nox_audiobook\lock (точные версии пакетов, Python, драйвер, модели) — по нему
#      потом ставится чистая машина один в один, без сюрпризов от новых версий;
#   4) запускает студию: отдельное окно на http://127.0.0.1:8765 (наружу не видно, только этот компьютер).
# Ничего не удаляет и не трогает озвучку. Ручные правки живут в <книга>\studio, готовые части и главы — в <книга>\gotovo.
#
# Запуск:  powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\nox_studio.ps1"
# Ключи:   -Root D:\kniga2a (другая книга), -NoStart (только поставить и сделать ярлык),
#          -NewBook -Root D:\kniga4 (загрузить НОВУЮ книгу: откроется окно выбора файла docx/txt/epub/fb2; в папке
#          книги появятся чистый book.docx, отчёт import_report.txt и список действующих лиц roles_scan.tsv).
param([string]$Root = "D:\kniga1a", [switch]$NoStart, [switch]$NewBook, [string]$Import = "")
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
$dl  = "$env:USERPROFILE\Downloads"
$Src = "D:\nox_audiobook"
$App = "$Src\studio"
$KIT = "NOX_STUDIO 2026-11-11g"

# Браузер сохраняет повторные загрузки как "имя (1).ext" — самый свежий файл получает каноническое имя.
function Use-Newest($pattern) {
    $groups = Get-ChildItem "$dl\$pattern" -File -ErrorAction SilentlyContinue |
              Group-Object { ($_.Name -replace ' \(\d+\)(?=\.[^.]+$)', '') }
    foreach ($g in $groups) {
        $f = $g.Group | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($f.Name -ne $g.Name) { Move-Item $f.FullName (Join-Path $dl $g.Name) -Force; Write-Host ("  " + $f.Name + " -> " + $g.Name) }
    }
}
Write-Host "=== Нокс Студия: установка" -ForegroundColor Cyan
Use-Newest "nox_studio*.py"
Use-Newest "nox_studio*.html"
Use-Newest "nox_cues*.py"
Use-Newest "nox_import*.py"
New-Item -ItemType Directory -Force -Path $App | Out-Null
foreach ($f in @("nox_studio.py", "nox_studio.html", "nox_cues.py", "nox_import.py")) {
    if (Test-Path "$dl\$f") {
        if (-not (Select-String -Path "$dl\$f" -SimpleMatch $KIT -Quiet)) {
            Write-Host ("В Загрузках старая версия " + $f + " — скачайте её из последнего сообщения Claude. Ничего не изменено.") -ForegroundColor Red
            exit 1
        }
        Copy-Item "$dl\$f" "$App\$f" -Force
    }
    if (-not (Test-Path "$App\$f")) {
        Write-Host ("Нет файла " + $f + " ни в Загрузках, ни в " + $App + " — скачайте его из сообщения Claude.") -ForegroundColor Red
        exit 1
    }
}
$py = "$Root\envfast\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "$Src\envfast\Scripts\python.exe" }
if (-not (Test-Path $py)) {
    Write-Host ("Нет окружения озвучки (" + $py + "). Сначала запустите go — он его создаёт.") -ForegroundColor Red
    exit 1
}
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

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

# Переозвучка на лету идёт через nox_fast.py в папке книги: он должен быть новой версии (понимает заявки студии).
# Прежние воркеры, если они сейчас работают, от замены файла не страдают — они его уже загрузили.
Use-Newest "nox_fast*.py"
# Инструменты книги, которыми пользуется студия: банк голосов и подбор (кнопка «Голоса»), самопроверка ударений,
# сборка по частям. Обычно их кладёт go; здесь — на случай, если студию обновили отдельно от go.
foreach ($f in @("nox_voices.py", "nox_stress.py", "nox_part.py")) {
    Use-Newest ($f -replace '\.py$', '*.py')
    if ((Test-Path "$dl\$f") -and (Select-String -Path "$dl\$f" -SimpleMatch "NOX_KIT 2026-11-11g" -Quiet) -and (Test-Path "$Root\nox_audiobook.py")) {
        Copy-Item "$dl\$f" "$Root\$f" -Force
    }
}
$rootFast = "$Root\nox_fast.py"
$hasQueue = (Test-Path $rootFast) -and (Select-String -Path $rootFast -SimpleMatch "studio_queue" -Quiet)
if (-not $hasQueue) {
    if ((Test-Path "$dl\nox_fast.py") -and (Select-String -Path "$dl\nox_fast.py" -SimpleMatch "studio_queue" -Quiet)) {
        Copy-Item "$dl\nox_fast.py" $rootFast -Force
        Write-Host "  nox_fast.py в папке книги обновлён: студия сможет переозвучивать фразы на лету"
    } else {
        Write-Host "  В папке книги прежний nox_fast.py: слушать и править можно, а переозвучка на лету заработает," -ForegroundColor Yellow
        Write-Host "  когда скачаете новый nox_fast.py и повторите эту команду (или запустите go с новыми файлами)." -ForegroundColor Yellow
    }
}
if (-not (Test-Path "$Root\work\segments.csv")) {
    Write-Host ("Книга в " + $Root + " ещё не подготовлена (нет work\segments.csv). Сначала запустите go.") -ForegroundColor Red
    exit 1
}

# значок и ярлыки (рабочий стол + меню «Пуск»)
& $py "$App\nox_studio.py" --icon "$App\nox_studio.ico"
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
New-Item -ItemType Directory -Force -Path $lock | Out-Null
$snapPy = @'
import json, os, platform, subprocess, sys, time
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
for name in ("torch", "torchaudio", "transformers", "faster_qwen3_tts", "qwen_tts", "faster_whisper", "ctranslate2",
             "ruaccent", "onnxruntime", "numpy", "soundfile", "librosa", "docx"):
    try:
        mod = __import__(name)
        man["packages_key"][name] = getattr(mod, "__version__", "?")
    except Exception as e:
        man["packages_key"][name] = "нет (" + type(e).__name__ + ")"
try:
    import torch
    man["cuda"] = {"available": torch.cuda.is_available(), "torch_cuda": torch.version.cuda}
except Exception:
    pass
(out / "envfast.lock.txt").write_text(run([sys.executable, "-m", "pip", "freeze"]) + "\n", encoding="utf-8")
env_py = root / "env" / "python.exe"
if env_py.exists():
    (out / "env.lock.txt").write_text(run([str(env_py), "-m", "pip", "freeze"]) + "\n", encoding="utf-8")
    man["python_env"] = run([str(env_py), "-c", "import sys; print(sys.version.split()[0])"])
(out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
print("  снимок окружения:", out, "(пакетов в envfast:", len((out / "envfast.lock.txt").read_text(encoding="utf-8").splitlines()), ")")
'@
[IO.File]::WriteAllText("$lock\snapshot.py", $snapPy, (New-Object System.Text.UTF8Encoding $false))
$env:HF_HOME = "$Root\hf"
& $py "$lock\snapshot.py" $Src $Root $lock $KIT
if ($LASTEXITCODE -ne 0) { Write-Host "  снимок окружения не снят — студии это не мешает" -ForegroundColor Yellow }

if ($NoStart) {
    if ($env:NOX_FROM_GO -eq "1") { Write-Host "=== Редактор установлен. Окно откроется само, когда go соберёт часть (пока идёт озвучка, окна нет — так и должно быть); позже — ярлыком «Nox Studio» на рабочем столе." -ForegroundColor Green }
    else { Write-Host "=== Готово. Запуск — ярлыком «Nox Studio»." -ForegroundColor Green }
    exit 0
}
Write-Host ("=== Запускаю студию для книги " + $Root) -ForegroundColor Cyan
Start-Process -FilePath $pyw -ArgumentList ('"' + "$App\nox_studio.py" + '" --root "' + $Root + '"') -WorkingDirectory $Root
Write-Host "  окно откроется через несколько секунд. Если не открылось — журнал: $Root\studio\studio.log"
Write-Host "  если книга сейчас озвучивается воркерами прежней версии, они заявки студии не берут: запустите go ещё раз"
