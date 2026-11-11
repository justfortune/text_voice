# ustanovka_s_nulya.ps1 — NOX_SETUP 2026-11-11a. Общая среда Nox Audiobook на чистом компьютере: D:\nox_audiobook с нуля.
# Каждый шаг пропускается, если его результат уже есть. Ничего существующего не переустанавливает и не удаляет.
#   1) папки D:\nox_audiobook: env, envfast, hf, pip_cache, tools, tmp;
#   2) env — Python 3.12 (conda, если она есть, иначе установщик python.org) + torch 2.11.0 cu128 + qwen-tts 0.1.1;
#   3) envfast — окружение озвучки: faster-qwen3-tts 0.5.4, ruaccent, faster-whisper, silero-stress, openpyxl;
#   4) ffmpeg в tools\ffmpeg;
#   5) модели Qwen3-TTS (токенизатор, VoiceDesign, Base, около 8 ГБ) в hf; модель с ударениями докачает подготовка книги;
#   6) исходник конвейера base\nox_audiobook.py -> D:\nox_audiobook\nox_audiobook.py;
#   7) комплект, сборщик и словарь -> Загрузки (папку kit_nox_<версия> сборщик с ключом -Fix разложит сам).
# Версии пакетов — те, на которых конвейер работает: data\sreda_*.txt (снимок рабочей машины), иначе data\versii_*.txt.
# Запуск из папки репозитория:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\ustanovka_s_nulya.ps1
# Ключи: -Plan — только показать, что будет сделано; -BezModeley — не качать модели заранее; -Src — другая папка среды.
# ВНИМАНИЕ: собран из шагов, которыми среда ставилась на рабочей машине; целиком на чистом компьютере не запускался.
param([string]$Src = "D:\nox_audiobook", [switch]$Plan, [switch]$BezModeley, [string]$Downloads = "")
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { chcp 65001 | Out-Null } catch {}
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"; $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)      # папка репозитория (над tools)
if (-not $Downloads) { $Downloads = Join-Path $env:USERPROFILE "Downloads" }
$TORCH_VER = "2.11.0"; $TORCH_INDEX = "https://download.pytorch.org/whl/cu128"; $DRIVER_MIN = 570
$BASE_SHA = "4844E7C3AB344B702EA403DA08E4F1E346030CDA"
$PY_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$FF_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
$script:did = 0; $script:skipped = 0

function Say([string]$text, [string]$color = "") { if ($color) { Write-Host $text -ForegroundColor $color } else { Write-Host $text } }
function Step([string]$text) { Say ""; Say ("=== " + $text) "Cyan" }
function Skip([string]$text) { $script:skipped++; Say ("  уже есть: " + $text) }
function Die([string]$text) { Say ""; Say ("СТОП: " + $text) "Red"; exit 1 }
function Run([string]$exe, [string[]]$arguments) {
    Say ("  > " + $exe + " " + ($arguments -join " ")) "DarkGray"
    $script:did++
    if ($Plan) { return }
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ("команда завершилась с ошибкой " + $LASTEXITCODE + ": " + $exe + " " + ($arguments -join " ")) }
}
function Download([string]$url, [string]$dest) {
    Say ("  скачиваю " + $url)
    $script:did++
    if ($Plan) { return }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
}
function PyOk([string]$py, [string]$code) {
    if (-not (Test-Path -LiteralPath $py)) { return $false }
    & $py -c $code *> $null
    return ($LASTEXITCODE -eq 0)
}
function Sha1Norm([string]$p) {
    try {
        $bytes = [IO.File]::ReadAllBytes($p) | Where-Object { $_ -ne 13 }
        $sha = [Security.Cryptography.SHA1]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash([byte[]]$bytes)) -replace '-', '')
    } catch { return "" }
}
# Файл ограничений pip: только строки «имя==версия» из снимка рабочей машины (или из списка ключевых версий)
function Constraints([string]$which) {
    $full = Join-Path $RepoDir ("data/sreda_" + $which + ".txt"); $key = Join-Path $RepoDir ("data/versii_" + $which + ".txt")
    $from = ""
    if (Test-Path -LiteralPath $full) { $from = $full } elseif (Test-Path -LiteralPath $key) { $from = $key }
    if (-not $from) { Say "  списка версий в data нет — пакеты встанут последних версий" "Yellow"; return "" }
    $lines = @(Get-Content -LiteralPath $from -Encoding UTF8 | Where-Object { $_ -match '^[A-Za-z0-9_.\-]+==[^\s;@]+$' })
    Say ("  версии пакетов — из " + (Split-Path $from -Leaf) + " (строк: " + $lines.Count + ")")
    $out = Join-Path (Join-Path $Src "tmp") ("ogranicheniya_" + $which + ".txt")
    if (-not $Plan) { [IO.File]::WriteAllLines($out, [string[]]$lines, (New-Object Text.UTF8Encoding $false)) }
    return $out
}

try {
    Say ("Общая среда Nox Audiobook -> " + $Src + $(if ($Plan) { "   [ключ -Plan: только показываю шаги, ничего не делаю]" } else { "" })) "Cyan"
    foreach ($f in @("base/nox_audiobook.py", "kit/go_kniga1a.ps1", "kit/run_fast.ps1", "tools/sborshchik_k1a.ps1")) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoDir $f))) { Die ("рядом нет " + $f + " — запускайте этот файл из папки репозитория (tools\ustanovka_s_nulya.ps1)") }
    }
    $kitVer = ""
    if ((Get-Content -LiteralPath (Join-Path $RepoDir "kit/go_kniga1a.ps1") -TotalCount 80 -Encoding UTF8 | Out-String) -match 'NOX_KIT (\d{4}-\d\d-\d\d[a-z]?)') { $kitVer = $Matches[1] }

    # ------------------------------------------------------------------ 0. что должно быть на компьютере
    Step "0. Компьютер"
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        if ($Plan) { Say "  ! nvidia-smi не найден: на этом компьютере нет драйвера NVIDIA (для плана это не важно)" "Yellow" }
        else { Die "nvidia-smi не найден: поставьте драйвер NVIDIA (нужна видеокарта NVIDIA с 24 ГБ памяти) и запустите снова" }
    } else {
        $gpu = "$(& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | Select-Object -First 1)".Trim()
        Say ("  видеокарта: " + $gpu)
        $drv = 0; try { $drv = [int](($gpu -split ',')[1].Trim().Split(".")[0]) } catch {}
        if ($drv -gt 0 -and $drv -lt $DRIVER_MIN) { Die ("драйвер NVIDIA " + $drv + " слишком старый для torch cu128 — обновите драйвер (нужен " + $DRIVER_MIN + " или новее)") }
    }
    $drive = Split-Path -Qualifier $Src -ErrorAction SilentlyContinue
    if ($drive) {
        $di = Get-PSDrive -Name $drive.TrimEnd(":") -ErrorAction SilentlyContinue
        if ($di -and $di.Free) {
            $gb = [math]::Round($di.Free / 1GB, 1)
            Say ("  свободно на " + $drive + " " + $gb + " ГБ" + $(if ($gb -lt 45) { " — МАЛО: среда, модели и кэш pip займут около 40 ГБ" } else { "" })) $(if ($gb -lt 45) { "Yellow" } else { "" })
        }
    }

    # ------------------------------------------------------------------ 1. папки
    Step "1. Папки"
    foreach ($d in @($Src, (Join-Path $Src "tmp"), (Join-Path $Src "hf"), (Join-Path $Src "pip_cache"), (Join-Path $Src "tools"))) {
        if (Test-Path -LiteralPath $d) { continue }
        Say ("  + " + $d); $script:did++
        if (-not $Plan) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    }
    $env:TEMP = Join-Path $Src "tmp"; $env:TMP = $env:TEMP          # pip распаковывает torch (около 3 ГБ) во временную папку
    $env:HF_HOME = Join-Path $Src "hf"; $env:PIP_CACHE_DIR = Join-Path $Src "pip_cache"

    # ------------------------------------------------------------------ 2. env: Python 3.12 + torch + qwen-tts
    Step "2. Окружение env (подготовка книги, создание образцов голосов)"
    $EnvPy = Join-Path (Join-Path $Src "env") "python.exe"
    $cudaCheck = "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)"
    if (PyOk $EnvPy "import torch, qwen_tts, docx, soundfile, num2words, huggingface_hub; assert torch.cuda.is_available()") { Skip "env с torch и qwen-tts, видеокарта видна" }
    else {
        if (-not (Test-Path -LiteralPath $EnvPy)) {
            $condaExe = $env:CONDA_EXE
            if (-not $condaExe) {
                foreach ($c in @("$env:USERPROFILE\anaconda3\Scripts\conda.exe", "$env:USERPROFILE\miniconda3\Scripts\conda.exe", "$env:LOCALAPPDATA\anaconda3\Scripts\conda.exe",
                                 "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe", "$env:ProgramData\anaconda3\Scripts\conda.exe", "$env:ProgramData\miniconda3\Scripts\conda.exe",
                                 "C:\anaconda3\Scripts\conda.exe", "C:\miniconda3\Scripts\conda.exe")) { if (Test-Path -LiteralPath $c) { $condaExe = $c; break } }
            }
            if ($condaExe) {
                Say "  conda найдена: окружение Python 3.12 (так стоит на рабочей машине)"
                $env:CONDA_PKGS_DIRS = Join-Path $Src "conda_pkgs"
                Run $condaExe @("create", "-y", "-p", (Join-Path $Src "env"), "-c", "conda-forge", "--override-channels", "python=3.12", "pip")
            } else {
                Say "  conda нет: ставлю Python 3.12.10 с python.org прямо в папку env"
                $inst = Join-Path (Join-Path $Src "tmp") "python-3.12.10-amd64.exe"
                if (-not (Test-Path -LiteralPath $inst)) { Download $PY_URL $inst }
                Say ("  > " + $inst + " /quiet TargetDir=" + (Join-Path $Src "env")) "DarkGray"; $script:did++
                if (-not $Plan) {
                    $p = Start-Process -FilePath $inst -ArgumentList @("/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_test=0", "Include_launcher=0", "AssociateFiles=0", "Shortcuts=0",
                                                                       ("TargetDir=" + (Join-Path $Src "env")), "/log", (Join-Path (Join-Path $Src "tmp") "python_install.log")) -Wait -PassThru
                    if (-not (Test-Path -LiteralPath $EnvPy)) { throw ("Python не установился (код " + $p.ExitCode + "), см. tmp\python_install.log") }
                }
            }
        }
        $cons = Constraints "env"
        Run $EnvPy @("-m", "pip", "install", "-U", "pip", "wheel", "setuptools")
        Run $EnvPy @("-m", "pip", "install", ("torch==" + $TORCH_VER), "torchaudio", "--index-url", $TORCH_INDEX)
        $pkgs = @("-m", "pip", "install", "qwen-tts==0.1.1", "transformers==4.57.3", "python-docx", "soundfile", "numpy", "num2words", "tqdm", "huggingface_hub", "--extra-index-url", $TORCH_INDEX)
        if ($cons) { $pkgs += @("-c", $cons) }
        Run $EnvPy $pkgs
        if (-not $Plan) {
            if (-not (PyOk $EnvPy $cudaCheck)) {
                Say "  torch подменился сборкой без видеокарты — возвращаю сборку cu128" "Yellow"
                Run $EnvPy @("-m", "pip", "install", "--force-reinstall", ("torch==" + $TORCH_VER), "torchaudio", "--index-url", $TORCH_INDEX)
                if (-not (PyOk $EnvPy $cudaCheck)) { throw "torch в env не видит видеокарту: обновите драйвер NVIDIA и запустите снова" }
            }
            Say "  + env готово: torch видит видеокарту" "Green"
        }
    }

    # ------------------------------------------------------------------ 3. envfast: окружение озвучки
    Step "3. Окружение envfast (озвучка, проверка распознаванием, студия)"
    $FPy = Join-Path (Join-Path (Join-Path $Src "envfast") "Scripts") "python.exe"
    if (PyOk $FPy "import torch, faster_qwen3_tts, qwen_tts, ruaccent, faster_whisper, silero_stress, openpyxl; assert torch.cuda.is_available()") { Skip "envfast с движком озвучки, ударениями и проверкой, видеокарта видна" }
    else {
        if (-not (Test-Path -LiteralPath $FPy)) { Run $EnvPy @("-m", "venv", (Join-Path $Src "envfast")) }
        $cons = Constraints "envfast"
        Run $FPy @("-m", "pip", "install", "-U", "pip")
        Run $FPy @("-m", "pip", "install", ("torch==" + $TORCH_VER), "torchaudio", "--index-url", $TORCH_INDEX)
        # transformers пригвождён к 5.15.1: в 5.16 и новее сломан Mimi-энкодер, на который опирается qwen-tts-hf
        $pkgs = @("-m", "pip", "install", "faster-qwen3-tts==0.5.4", "transformers==5.15.1", "soundfile", "numpy", "librosa", "python-docx", "num2words", "sox", "--extra-index-url", $TORCH_INDEX)
        if ($cons) { $pkgs += @("-c", $cons) }
        Run $FPy $pkgs
        $more = @("-m", "pip", "install", "ruaccent==1.5.8.3", "faster-whisper==1.2.1", "silero-stress==1.5", "openpyxl==3.1.5", "--extra-index-url", $TORCH_INDEX)
        if ($cons) { $more += @("-c", $cons) }
        try { Run $FPy $more } catch { Say ("  ! ударения и проверка не поставились заранее (" + $_.Exception.Message + ") — конвейер поставит их сам при первом запуске") "Yellow" }
        if (-not $Plan) {
            if (-not (PyOk $FPy "import torch, faster_qwen3_tts, qwen_tts; assert torch.cuda.is_available()")) {
                Say "  torch в envfast подменился сборкой без видеокарты — возвращаю сборку cu128" "Yellow"
                Run $FPy @("-m", "pip", "install", "--force-reinstall", ("torch==" + $TORCH_VER), "torchaudio", "--index-url", $TORCH_INDEX)
                if (-not (PyOk $FPy "import torch, faster_qwen3_tts, qwen_tts; assert torch.cuda.is_available()")) { throw "envfast не собрался: torch не видит видеокарту или не встал faster-qwen3-tts" }
            }
            Say "  + envfast готово" "Green"
        }
    }

    # ------------------------------------------------------------------ 4. ffmpeg
    Step "4. ffmpeg (сборка частей и готовых файлов)"
    $ffdir = Join-Path (Join-Path $Src "tools") "ffmpeg"
    $ffexe = $null
    if (Test-Path -LiteralPath $ffdir) { $ffexe = Get-ChildItem -LiteralPath $ffdir -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if ($ffexe) { Skip ("ffmpeg: " + $ffexe.FullName) }
    else {
        $zipF = Join-Path (Join-Path $Src "tmp") "ffmpeg.zip"
        Download $FF_URL $zipF
        Say ("  распаковываю в " + $ffdir)
        if (-not $Plan) {
            Expand-Archive -LiteralPath $zipF -DestinationPath $ffdir -Force -ErrorAction Stop
            $ffexe = Get-ChildItem -LiteralPath $ffdir -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            if (-not $ffexe) { throw ("ffmpeg не распаковался: положите ffmpeg.exe в " + $ffdir) }
            Say "  + ffmpeg на месте" "Green"
        }
    }

    # ------------------------------------------------------------------ 5. модели
    Step "5. Модели Qwen3-TTS (токенизатор, VoiceDesign, Base — около 8 ГБ)"
    if ($BezModeley) { Say "  ключ -BezModeley: модели скачаются сами при первом обращении" }
    else {
        $hub = Join-Path (Join-Path $Src "hf") "hub"
        $want = @("Qwen/Qwen3-TTS-Tokenizer-12Hz", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
        $missing = @($want | Where-Object { -not (Test-Path -LiteralPath (Join-Path $hub ("models--" + ($_ -replace '/', '--')))) })
        if ($missing.Count -eq 0) { Skip "все три модели в hf\hub" }
        else {
            $pre = "import sys, huggingface_hub as h`nfor m in sys.argv[1:]:`n    try:`n        print(m, '->', h.snapshot_download(m))`n    except Exception as e:`n        print('не скачалась заранее (докачается при запуске):', m, type(e).__name__, str(e)[:120])`n"
            $preF = Join-Path (Join-Path $Src "tmp") "predownload.py"
            if (-not $Plan) { [IO.File]::WriteAllText($preF, $pre, (New-Object Text.UTF8Encoding $false)) }
            Run $EnvPy (@($preF) + $missing)
        }
        Say "  модель с ударениями (shib79/qwen3-tts-12hz-1.7b-ru-stress-cf, закреплённая версия) скачает подготовка книги при первом go"
    }

    # ------------------------------------------------------------------ 6. исходник конвейера
    Step "6. Исходник конвейера"
    $srcPy = Join-Path $Src "nox_audiobook.py"; $basePy = Join-Path $RepoDir "base/nox_audiobook.py"
    if ((Sha1Norm $basePy) -ne $BASE_SHA) { Die "base\nox_audiobook.py в репозитории повреждён (не та контрольная сумма) — скачайте репозиторий заново" }
    if (Test-Path -LiteralPath $srcPy) {
        if ((Sha1Norm $srcPy) -eq $BASE_SHA) { Skip "nox_audiobook.py в папке среды — целый исходник" }
        else { Say ("  ! " + $srcPy + " уже есть и отличается от исходника — не трогаю (сборщик с ключом -Fix умеет вернуть целый)") "Yellow" }
    } else {
        Say ("  + " + $srcPy); $script:did++
        if (-not $Plan) { Copy-Item -LiteralPath $basePy -Destination $srcPy -ErrorAction Stop }
    }

    # ------------------------------------------------------------------ 7. комплект — в Загрузки
    Step "7. Комплект и сборщик -> Загрузки"
    $goDl = Join-Path $Downloads "go_kniga1a.ps1"
    $have = ""
    if (Test-Path -LiteralPath $goDl) { if ((Get-Content -LiteralPath $goDl -TotalCount 80 -Encoding UTF8 | Out-String) -match 'NOX_KIT (\d{4}-\d\d-\d\d[a-z]?)') { $have = $Matches[1] } }
    $kitDir = Join-Path $Downloads ("kit_nox_" + $kitVer)
    $inDir = ""
    $goKit = Join-Path $kitDir "go_kniga1a.ps1"
    if (Test-Path -LiteralPath $goKit) { if ((Get-Content -LiteralPath $goKit -TotalCount 80 -Encoding UTF8 | Out-String) -match 'NOX_KIT (\d{4}-\d\d-\d\d[a-z]?)') { $inDir = $Matches[1] } }
    if ($have -and $have -eq $kitVer) { Skip ("комплект " + $kitVer + " уже разложен в Загрузках") }
    elseif ($inDir -and $inDir -eq $kitVer) { Skip ("папка " + (Split-Path $kitDir -Leaf) + " с комплектом уже в Загрузках") }
    else {
        Say ("  + " + $kitDir + " (19 файлов комплекта; сборщик с ключом -Fix разложит их по местам)"); $script:did++
        if (-not $Plan) {
            New-Item -ItemType Directory -Force -Path $kitDir | Out-Null
            Copy-Item -Path (Join-Path (Join-Path $RepoDir "kit") "*") -Destination $kitDir -Force -ErrorAction Stop
        }
    }
    foreach ($pair in @(@("tools/sborshchik_k1a.ps1", "sborshchik_k1a.ps1"), @("data/accent_dict.csv", "accent_dict.csv"))) {
        $to = Join-Path $Downloads $pair[1]
        if (Test-Path -LiteralPath $to) { Skip ($pair[1] + " в Загрузках") }
        elseif (Test-Path -LiteralPath (Join-Path $RepoDir $pair[0])) {
            Say ("  + " + $to); $script:did++
            if (-not $Plan) { Copy-Item -LiteralPath (Join-Path $RepoDir $pair[0]) -Destination $to -ErrorAction Stop }
        }
    }

    Say ""
    if ($Plan) { Say ("ПЛАН: шагов к выполнению " + $script:did + ", уже готово " + $script:skipped + ". Ничего не сделано. Запустите без ключа -Plan, чтобы выполнить.") "Green" }
    elseif ($script:did -eq 0) { Say "Общая среда уже развёрнута: все шаги пропущены, ничего не менялось." "Green" }
    else { Say ("ГОТОВО: общая среда развёрнута в " + $Src + " (сделано шагов: " + $script:did + ", пропущено готовых: " + $script:skipped + ").") "Green" }
    Say "Дальше — книга:"
    Say "  1) положите в Загрузки рукопись kniga1.docx и файлы книги (сценарий ролей speaker_overrides_kniga1a.tsv, поправки ударений stress_fixes_kniga1a.tsv);"
    Say "     образцы голосов, роли и правки студии, если они сохранены с прежней машины, — в папку книги D:\kniga1a (work\voices, work\roles.json, studio);"
    Say "  2) powershell -NoProfile -ExecutionPolicy Bypass -File `"$Downloads\sborshchik_k1a.ps1`" -Fix"
    Say "  3) powershell -NoProfile -ExecutionPolicy Bypass -File `"$Downloads\go_kniga1a.ps1`""
    Say "Новая книга без этих файлов: go_kniga1a.ps1 -Root D:\имя_папки, рукопись — book.docx в этой папке; голоса подберутся из банка сами."
    exit 0
} catch {
    Say ""
    Say ("СТОП: " + $_.Exception.Message) "Red"
    Say "Сделанное сохранено: исправьте причину и запустите снова — готовые шаги пропустятся." "Red"
    exit 1
}
