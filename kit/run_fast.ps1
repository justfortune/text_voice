# run_fast.ps1 — быстрая озвучка (v7): движок faster-qwen3-tts с CUDA-графами вместо медленного цикла Python.
# NOX_KIT 2026-11-12a
# Та же книга, те же голоса, тот же wav-кэш — меняется только скорость шага генерации.
#
# Что делает сам:
#   1) мягко останавливает СТАРЫЕ воркеры synth (watch и всё остальное не трогает), убирает брошенные замки;
#   2) один раз создаёт отдельное окружение D:\nox_audiobook\envfast (чтобы не трогать рабочее env):
#      torch cu128 + faster-qwen3-tts (тянет qwen-tts-hf и transformers 5) — ~5–10 минут при первом запуске;
#   3) запускает N быстрых воркеров: №1 — в этом окне, остальные — скрыто, их лог в fast_w<N>.log
#      (смотреть: Get-Content D:\nox_audiobook\fast_w2.log -Wait -Tail 20);
#   4) по завершении всех печатает итог. Окно watch (части на Яндекс Диск) продолжает работать как раньше.
#
# Запуск:  powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\run_fast.ps1"
# Повторный запуск безопасен: готовое пропускается. Ctrl+C останавливает воркер №1; скрытые добьют своё
# (остановить все: Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? {$_.CommandLine -like '*nox_fast*'} | % { Stop-Process -Id $_.ProcessId })

param(
    [string]$Root    = "D:\nox_audiobook",
    [string] $Accent = "acute",         # acute: знак ударения для ru-stress модели; none: чистый текст
    [int]   $Workers = 2,          # быстрых воркеров на 3090/24 ГБ: 2 (можно 3, если смотреть за памятью)
    [switch]$Xvec,                 # режим «только тембр»: ещё быстрее, интонация чуть менее похожа
    [int]   $MaxNewTokens = 1500,
    [string] $Model = "shib79/qwen3-tts-12hz-1.7b-ru-stress-cf",
    [string] $FastPy = "",              # явный путь к nox_fast.py (комплект nox_all); иначе ищется рядом/в проекте
    [switch]$ByParts                    # работа по частям: воркеры озвучивают одну часть и выходят (сообщения — под этот режим)
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$env:PYTORCH_CUDA_ALLOC_CONF = $null          # expandable_segments на этой сборке torch/Windows не поддерживается
$env:PIP_CACHE_DIR = "$Root\pip_cache"
$env:HF_HOME = "$Root\hf"

function Step($msg) { Write-Host ""; Write-Host ("=== " + $msg) -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$arguments) {
    Write-Host ("> " + $exe + " " + ($arguments -join " ")) -ForegroundColor DarkGray
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ("Команда завершилась с ошибкой " + $LASTEXITCODE) }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    if (-not (Test-Path "$Root\nox_audiobook.py")) { throw "Нет $Root\nox_audiobook.py" }
    if (-not (Test-Path "$Root\work\segments.csv")) { throw "Нет $Root\work\segments.csv — сначала segment" }
    Set-Location $Root
    try { Start-Transcript -Path "$Root\log_fast.txt" -Append | Out-Null } catch {}   # запуск из студии (без окна) — без протокола

    # ------------------------------------------------------------- свежий nox_fast.py (отсюда или из Загрузок)
    $src = @()
    foreach ($dir in @($ScriptDir, $Root)) {
        if (Test-Path $dir) { $src += Get-ChildItem -Path $dir -Filter "nox_fast*.py" -ErrorAction SilentlyContinue }
    }
    $pick = $src | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($FastPy -and (Test-Path -LiteralPath $FastPy)) { $pick = Get-Item -LiteralPath $FastPy }
    if (-not $pick) { throw "Рядом или в Загрузках нет nox_fast.py" }
    if ($pick.FullName -ne "$Root\nox_fast.py") { Copy-Item $pick.FullName "$Root\nox_fast.py" -Force }
    Write-Host ("скрипт воркера: " + $pick.FullName)

    # ------------------------------------------------------------- 1. остановить старые synth-воркеры (не watch)
    Step "Старые воркеры synth"
    $old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*nox_audiobook.py*synth*" }
    if ($old) {
        foreach ($pr in $old) {
            Write-Host ("  останавливаю PID " + $pr.ProcessId + " (" + $pr.CommandLine.Substring(0, [math]::Min(90, $pr.CommandLine.Length)) + "...)")
            Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep 3
    } else { Write-Host "  не найдено — хорошо" }

    # замки, брошенные упавшими/убитыми воркерами (сейчас synth не работает, поэтому можно убрать все)
    $fastRunning = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                   Where-Object { $_.CommandLine -like "*nox_fast*" }
    if (-not $fastRunning) {
        $locks = Get-ChildItem "$Root\work\wav\*.lock" -ErrorAction SilentlyContinue
        if ($locks) { $locks | Remove-Item -Force -ErrorAction SilentlyContinue; Write-Host ("  убрано замков: " + $locks.Count) }
    }

    # ------------------------------------------------------------- 2. окружение envfast
    $FPy = "$Root\envfast\Scripts\python.exe"
    if (-not (Test-Path $FPy)) {
        Step "Создаю окружение envfast (отдельно от env — рабочее не трогается)"
        Run "$Root\env\python.exe" @("-m", "venv", "$Root\envfast")
    }
    & $FPy -c "import torch, faster_qwen3_tts; import sys; sys.exit(0 if torch.cuda.is_available() else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Step "Ставлю пакеты в envfast: torch cu128 (~3 ГБ, может быть в кэше) + faster-qwen3-tts"
        Run $FPy @("-m", "pip", "install", "-U", "pip")
        Run $FPy @("-m", "pip", "install", "torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu128")
        Run $FPy @("-m", "pip", "install", "faster-qwen3-tts", "transformers==5.15.1", "soundfile", "numpy", "librosa", "python-docx", "num2words", "sox")
        # transformers пригвождён к 5.15.1: в 5.16+ сломан Mimi-энкодер, на который опирается qwen-tts-hf
        & $FPy -c "import torch; assert torch.cuda.is_available(), 'CUDA недоступна'; from faster_qwen3_tts import FasterQwen3TTS; import qwen_tts; print('envfast OK: torch', torch.__version__)"
        if ($LASTEXITCODE -ne 0) { throw "envfast не собрался — пришлите лог $Root\log_fast.txt" }
    } else { Write-Host "envfast уже готов — пропускаю установку" }

    # ------------------------------------------------------------- 3. воркеры
    Step ("Озвучка: " + $Workers + " быстрых воркера (CUDA-графы). Первый — здесь, лог остальных: fast_wN.log")
    # лимит VRAM на процесс не ставим: у CUDA-графов память предсказуема (~6.5 ГБ на воркер), а при 3 воркерах
    # доля 1/3 (7.2 ГБ) слишком близка к реальной и на пиках давала бы ошибки сегментов
    $frac = ""
    $common = @("$Root\nox_fast.py", "--model", $Model, "--accent", $Accent, "--max-new-tokens", "$MaxNewTokens")
    if ($Xvec) { $common += "--xvec" }
    if ($frac -ne "") { $common += @("--mem-fraction", $frac) }
    # для редактора «Нокс Студия»: этими же ключами он запускает движок переозвучки, когда озвучка книги не идёт
    try {
        $fa = @{ model = $Model; accent = $Accent; max_new_tokens = $MaxNewTokens; xvec = [bool]$Xvec } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText("$Root\work\fast_args.json", $fa, (New-Object System.Text.UTF8Encoding $false))
    } catch {}

    $procs = @()
    for ($i = 2; $i -le $Workers; $i++) {
        # все воркеры идут с начала книги: ранние части доделываются первыми (отбракованное проверкой позади —
        # каждые 15 с первым), часть собирается один раз, когда проверены все её реплики
        $wargs = $common + @("--worker", "$i")
        $dir = "с начала книги"
        $procs += Start-Process -FilePath $FPy -ArgumentList $wargs -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
                                -RedirectStandardOutput "$Root\fast_w$i.log" -RedirectStandardError "$Root\fast_w$i.err.log"
        Write-Host ("  воркер " + $i + " запущен (" + $dir + "), лог: " + $Root + "\fast_w" + $i + ".log")
        Start-Sleep 20
    }
    & $FPy @($common + @("--worker", "1"))

    if ($procs.Count -gt 0) {
        Write-Host "жду остальные воркеры…"
        $procs | Wait-Process -ErrorAction SilentlyContinue
    }

    Step "Все воркеры завершились"
    if ($ByParts) {
        Write-Host "Работа по частям: воркеры озвучили текущую часть. Её собирает nox_part.py (ход — part.log в папке книги);"
        Write-Host "дальше — прослушать и закрепить часть в «Нокс Студии», следующую часть озвучит следующий запуск go."
        return
    }
    Write-Host "Если окно watch работает — оно само соберёт оставшиеся части, целую книгу book.mp3 и отправит на Яндекс Диск."
    Write-Host "Если watch НЕ запущен, его команда:"
    Write-Host ('  & "' + $Root + '\env\python.exe" "' + $Root + '\nox_audiobook.py" watch --parts 19 --dest "$env:USERPROFILE\YandexDisk" --format mp3 --title "Посох Забвения" --author "Д."') -ForegroundColor Yellow
}
catch {
    Write-Host ""
    Write-Host ("ОШИБКА: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ("подробности: " + $Root + "\log_fast.txt")
    exit 1
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
