# go_kniga1a.ps1 — ОДНА КНОПКА: подготовка -> голоса -> окно отправки на Диск -> озвучка аудиоредакции.
# NOX_KIT 2026-11-18d
# Нужны в Загрузках: prep_kniga1a.ps1, run_fast.ps1, nox_fast.py, accent_dict.csv, voices_kniga1a.py, voicecheck_k1a.py,
#   verify_k1a.py (проверка каждой реплики распознаванием речи),
#   speaker_overrides.tsv (сценарий «кто говорит»), nastroyki_ozvuchki.py (таблица настроек), kniga1.docx.
#   Повторные загрузки «имя (1).ext» подхватываются сами.
# По умолчанию: модель Qwen3-TTS ru-stress (читает знак ударения) + разметка ударений ruaccent/словарь.
# Ключ -Base: базовая модель и чистый текст (вариант C). Ключ -Sparse: ударения только у имён, контекстных и длинных слов (D3).
# ВСЕ НАСТРОЙКИ (скорость, паузы, роли, пол/возраст голосов, части, воркеры) — в таблице «Настройки озвучки.xlsx»
# в папке книги. Правите таблицу, сохраняете, запускаете go — он делает минимум работы под изменения.
# Ключи (необязательно): -Root D:\kniga2a (вторая книга), -Root D:\kniga3a (третья), -Tempo 1.2 (разово поверх таблицы), -Base / -Sparse,
#   -PrepOnly (всё, кроме озвучки), -NoGpu (только подготовка без видеокарты: можно параллельно с озвучкой другой книги).
#   -Probe (подготовка + опыт probe_k1a.py: какой способ озвучки даёт меньше брака + сверка ударений stress_check.py; без озвучки).
#   -Map (только освежить разметку книги, карту ролей и времени, отчёт о голосах — razmetka_k1a.py; ничего не останавливает).
# РАБОТА ПО ЧАСТЯМ (по умолчанию). go озвучивает, проверяет распознаванием и собирает ОДНУ часть — первую, которую автор
#   ещё не закрепил в «Нокс Студии», сверяет в ней ударения второй моделью, открывает студию — и останавливается.
#   Закреплённые части не трогаются вовсе; дальше текущей части ничего не озвучивается, не проверяется и не разбирается.
#   Прослушали часть, закрепили — следующий go (или кнопка «Озвучить следующую часть» в студии) берёт следующую.
#   -All — прежний режим «вся книга сразу» (закреплённые части не трогаются и в нём). -NoStudio — не открывать студию в конце.
#   -Status — только сказать, где книга сейчас: сколько частей закреплено, что с текущей, сколько осталось. Ничего не запускает.
# НОВАЯ КНИГА: go -Root D:\имя_папки, рукопись — book.docx в этой папке. Голоса ролям подбираются из банка голосов сами
#   (nox_voices.py), сменить их можно в студии (кнопка «Голоса»); записывать свои голоса не нужно.
param([switch]$Base, [switch]$Sparse, [switch]$PrepOnly, [switch]$NoGpu, [switch]$Probe, [switch]$Map, [switch]$All, [switch]$NoStudio, [switch]$Status, [string]$Pack = "", [double]$Tempo = 0, [string]$Root = "D:\kniga1a", [string]$NoxHome = "", [string]$Dict = "", [string]$Overrides = "", [string]$StressFixes = "")
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
$Inputs = "$env:USERPROFILE\Downloads"
$dl = $Inputs
$Src = if ($NoxHome) { $NoxHome } else { "D:\nox_audiobook" }
$releaseInfo = $null
$releaseFile = Join-Path $Root "work\nox_release.json"
if (Test-Path -LiteralPath $releaseFile) {
    try { $releaseInfo = Get-Content -LiteralPath $releaseFile -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json }
    catch { Write-Host "Не читается запись установленного комплекта. Файлы книги не менялись." -ForegroundColor Red; exit 1 }
    if (-not $NoxHome -and $releaseInfo.home) { $Src = [string]$releaseInfo.home }
    if (-not $Pack -and $releaseInfo.kit_dir) { $Pack = [string]$releaseInfo.kit_dir }
}
if (-not $Pack -and (Test-Path -LiteralPath (Join-Path $Src 'studio\nox_release.json'))) {
    try { $sharedRelease = Get-Content -LiteralPath (Join-Path $Src 'studio\nox_release.json') -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json }
    catch { Write-Host "Не читается запись общего комплекта; запуск остановлен." -ForegroundColor Red; exit 1 }
    $Pack = [string]$sharedRelease.kit_dir
}
if (-not $Pack -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot "nox_update.py"))) { $Pack = $PSScriptRoot }
if ($Pack) { $dl = $Pack }
$PyFast = "$Root\envfast\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PyFast)) { $PyFast = "$Src\envfast\Scripts\python.exe" }

# Статус выполняется до любых копирований, переименований, pip и записи настроек.
if ($Status) {
    $statusScript = Join-Path $dl "nox_status.py"
    if (-not (Test-Path -LiteralPath $statusScript)) { $statusScript = Join-Path $Root "nox_status.py" }
    if (-not (Test-Path -LiteralPath $statusScript) -or -not (Test-Path -LiteralPath $PyFast)) {
        Write-Host "Нет установленного средства чтения статуса или Python; подготовка не запускалась." -ForegroundColor Red
        exit 1
    }
    & $PyFast -B $statusScript --root $Root
    exit $LASTEXITCODE
}
if ($Pack -and -not $releaseInfo -and (Test-Path -LiteralPath "$Root\work\segments.csv")) {
    Write-Host "Подготовленная книга ещё не обновлена целым комплектом 18d. Сначала выполните Update_NOX_18d.ps1 -Check; go ничего не менял." -ForegroundColor Red
    exit 1
}
if ($Pack) {
    $packageRoot = Split-Path -Parent $Pack
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "release_manifest.json"))) {
        Write-Host "Нет манифеста комплекта 18d. Используйте полный проверенный архив." -ForegroundColor Red
        exit 1
    }
    & $PyFast -B (Join-Path $Pack "nox_update.py") --root $Root --home $Src --bundle $packageRoot --verify-bundle
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

# Браузер сохраняет повторные загрузки как "имя (1).ext" — берём самый свежий файл и даём ему каноническое имя,
# чтобы старая версия не запускалась по ошибке.
function Use-Newest($pattern, $canon) {
    if ($Pack) { return }  # неизменяемый комплект уже проверен по SHA-256
    # «имя (1).ext», «имя (2).ext» -> самое свежее становится «имя.ext»; разные имена (speaker_overrides_kniga2a.tsv
    # и speaker_overrides.tsv) — разные семейства и друг друга не затирают
    $groups = Get-ChildItem "$dl\$pattern" -File -ErrorAction SilentlyContinue |
              Group-Object { ($_.Name -replace ' \(\d+\)(?=\.[^.]+$)', '') }
    foreach ($g in $groups) {
        $f = $g.Group | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($f.Name -ne $g.Name) { Move-Item $f.FullName (Join-Path $dl $g.Name) -Force; Write-Host ("  " + $f.Name + " -> " + $g.Name) }
    }
}
Use-Newest "prep_kniga1a*.ps1" "prep_kniga1a.ps1"
Use-Newest "run_fast*.ps1" "run_fast.ps1"
Use-Newest "nox_fast*.py" "nox_fast.py"
Use-Newest "voices_*.py" ""
Use-Newest "voicecheck_k1a*.py" "voicecheck_k1a.py"
Use-Newest "speaker_overrides*.tsv" ""
Use-Newest "accent_dict*.csv" "accent_dict.csv"
Use-Newest "nastroyki_ozvuchki*.py" "nastroyki_ozvuchki.py"
Use-Newest "verify_k1a*.py" "verify_k1a.py"
Use-Newest "diag_k1a*.py" "diag_k1a.py"
Use-Newest "probe_k1a*.py" "probe_k1a.py"
Use-Newest "stress_check*.py" "stress_check.py"
Use-Newest "razmetka_k1a*.py" "razmetka_k1a.py"
Use-Newest "zvuk_k1a*.py" "zvuk_k1a.py"
Use-Newest "stress_fixes*.tsv" ""
Use-Newest "nox_studio*.py" "nox_studio.py"
Use-Newest "nox_studio*.html" "nox_studio.html"
Use-Newest "nox_studio*.ps1" "nox_studio.ps1"
Use-Newest "nox_part*.py" "nox_part.py"
Use-Newest "nox_stress*.py" "nox_stress.py"
Use-Newest "nox_voices*.py" "nox_voices.py"
# Все файлы комплекта должны быть одной версии: старый файл в Загрузках (не скачался) — стоп, с понятным списком
$KIT = "NOX_KIT 2026-11-18d"
$env:NOX_FROM_GO = "1"      # prep и установщик студии узнают, что их запустил go: их советы для ручного запуска не печатаются
$kitFiles = @("prep_kniga1a.ps1", "nox_fast.py", "verify_k1a.py", "voicecheck_k1a.py", "nastroyki_ozvuchki.py", "razmetka_k1a.py", "zvuk_k1a.py",
              "nox_part.py", "nox_stress.py", "nox_voices.py")
$stale = @($kitFiles | Where-Object { -not (Test-Path "$dl\$_") -or -not (Select-String -Path "$dl\$_" -SimpleMatch $KIT -Quiet) })
if ($stale.Count -gt 0) {
    Write-Host ("В Загрузках старая версия (или нет файла): " + ($stale -join ", ")) -ForegroundColor Red
    Write-Host "Скачайте эти файлы из последнего сообщения Claude и запустите снова. Ничего не изменено." -ForegroundColor Red
    exit 1
}
Write-Host ("  комплект файлов: " + $KIT)
$ByParts = -not $All
$leafB = (Split-Path $Root -Leaf).ToLower()
$IsBook1 = ($leafB -like "kniga1*")
# Разметка книги тегами, карта «кто за кем говорит» и времени, отчёт о голосах (razmetka_k1a.py; GPU не нужен)
function Run-Map {
    foreach ($f in @("razmetka_k1a.py", "nox_fast.py")) { if (Test-Path "$dl\$f") { Copy-Item "$dl\$f" "$Root\$f" -Force } }
    if ((Test-Path "$Root\razmetka_k1a.py") -and (Test-Path "$Root\nox_audiobook.py") -and (Test-Path "$Root\work\segments.csv")) {
        & $PyFast "$Root\razmetka_k1a.py"
        if ($LASTEXITCODE -ne 0) { Write-Host "  разметка не создана — пришлите вывод в чат (озвучке это не мешает)" -ForegroundColor Yellow }
    } else { Write-Host "  разметка: книга ещё не подготовлена — будет при обычном запуске go" -ForegroundColor Yellow }
}
if ($Map) {
    Write-Host "=== Разметка книги, карта ролей и времени, отчёт о голосах (ничего не останавливаю)" -ForegroundColor Cyan
    Run-Map
    exit 0
}

Write-Host "=== Настройки озвучки: $Root\Настройки озвучки.xlsx" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Root, "$Root\work" | Out-Null
if (Test-Path "$dl\nastroyki_ozvuchki.py") { Copy-Item "$dl\nastroyki_ozvuchki.py" "$Root\nastroyki_ozvuchki.py" -Force }
& $PyFast -c "import openpyxl" 2>$null
if ($LASTEXITCODE -ne 0) { if ($releaseInfo) { throw "В рабочем envfast не импортируется openpyxl. Пакеты не менялись." }; Write-Host "  ставлю openpyxl (таблицы Excel), разово"; & $PyFast -m pip install -q openpyxl }
$st = $null
if (Test-Path "$Root\nastroyki_ozvuchki.py") {
    & $PyFast "$Root\nastroyki_ozvuchki.py" read $Root
    try { $st = Get-Content "$Root\work\settings.json" -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $st = $null }
} else { Write-Host "  nastroyki_ozvuchki.py нет в Загрузках — беру прежние значения по умолчанию" -ForegroundColor Yellow }
function Cfg($name, $dflt) { if ($st -and ($null -ne $st.$name) -and ("$($st.$name)" -ne "")) { return $st.$name } else { return $dflt } }
$inv = [Globalization.CultureInfo]::InvariantCulture
$TempoV   = [double](Cfg "tempo" 1.12); if ($Tempo -gt 0) { $TempoV = $Tempo }
$TempoStr = $TempoV.ToString("0.000", $inv)
$PausesStr = ([double](Cfg "pauses" 0.85)).ToString("0.00", $inv)
$Workers  = [int](Cfg "workers" 3)
$PartMin  = [int](Cfg "part_min" 19)
$Fmt      = Cfg "format" "mp3"
$Bitrate  = Cfg "bitrate" "96k"
$Title    = Cfg "title" $(if ($IsBook1) { "Посох Забвения" } else { Split-Path $Root -Leaf })
$Author   = Cfg "author" "Д."
$DestName = Cfg "dest" $(if ($IsBook1) { "Posoh_izdanie3" } else { Split-Path $Root -Leaf })
# книга 1 — третье издание: своя папка на Диске; папка второго издания (Posoh_izdanie2) остаётся как есть
if ((Split-Path $Root -Leaf).ToLower() -eq "kniga1a" -and $DestName -eq "Posoh_izdanie2") { $DestName = "Posoh_izdanie3" }
if ($DestName -match '^[A-Za-z]:\\') { $DestBase = $DestName } else { $DestBase = "$env:USERPROFILE\YandexDisk\$DestName" }
# -All: каждый запуск go — своя папка внутри, с датой и временем начала запуска: прежние запуски на Диске не трогаются.
# По частям: черновик текущей части — в постоянную папку chernovik (готовые части кладёт студия в папку gotovo).
$RunStamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
if ($ByParts) { $Dest = $DestBase.TrimEnd('\') + '\chernovik' } else { $Dest = $DestBase.TrimEnd('\') + '\' + $RunStamp }
$MaxChars = [int](Cfg "max_chars" 200)
$DocxSet  = Cfg "docx" ""
$AccentSet = Cfg "accent" "acute"
$TrimArg  = if ((Cfg "trim" $true) -eq $false) { " --no-trim-pauses" } else { "" }
$EqArg    = if ((Cfg "equalize" $true) -eq $false) { " --no-equalize" } else { "" }
if ($Base -or $AccentSet -eq "base") { $Model = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"; $Accent = "none" }
elseif ($Sparse -or $AccentSet -eq "sparse") { $Model = "shib79/qwen3-tts-12hz-1.7b-ru-stress-cf"; $Accent = "sparse" }
else { $Model = "shib79/qwen3-tts-12hz-1.7b-ru-stress-cf"; $Accent = "acute" }
Write-Host ("=== Движок: " + $Model + "   ударения: " + $Accent + "   скорость речи: " + [int][math]::Round($TempoV * 100) + "%   воркеров: " + $Workers) -ForegroundColor Cyan
if ($ByParts) { Write-Host "=== Режим: ПО ЧАСТЯМ — одна часть за запуск; закреплённые в студии части не трогаются, дальше текущей ничего не делается" -ForegroundColor Cyan }
else { Write-Host "=== Режим: ВСЯ КНИГА СРАЗУ (-All); закреплённые в студии части не трогаются" -ForegroundColor Cyan }
# ключи сборки части — одни и те же для nox_part.py (по частям) и окна watch (-All); название и автор берутся из таблицы
$partArgs = @("--parts", "$PartMin", "--tempo", $TempoStr, "--pauses", $PausesStr, "--format", "$Fmt", "--bitrate", "$Bitrate")
if ($TrimArg) { $partArgs += "--no-trim-pauses" }
if ($EqArg) { $partArgs += "--no-equalize" }
function Part-Status {
    # над какой частью идёт работа: {part, closed, done_all, fixed, rows, have, checked, total}
    try {
        $j = & $PyFast "$Root\nox_part.py" --root $Root --status @partArgs 2>$null | Select-Object -Last 1
        return ($j | ConvertFrom-Json)
    } catch { return $null }
}

Write-Host "=== Останавливаю старые воркеры озвучки и прежнее окно отправки на Диск этой книги" -ForegroundColor Cyan
# только процессы ЭТОЙ книги (путь $Root в командной строке): озвучка другой книги продолжает работать
$rootPat = "*" + $Root.TrimEnd('\') + "\*"
if (-not $NoGpu) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $rootPat -and ($_.CommandLine -like "*nox_fast*" -or $_.CommandLine -like "*synth*" -or
                       $_.CommandLine -like "*watch*" -or $_.CommandLine -like "*verify_k1a*" -or $_.CommandLine -like "*zvuk_k1a*" -or
                       $_.CommandLine -like "*nox_part*" -or $_.CommandLine -like "*nox_stress*" -or $_.CommandLine -like "*nox_voices*") } |
        ForEach-Object { Write-Host ("  stop PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # пустое окно прежнего watch этой книги (powershell -NoExit) закрываем, чтобы не плодить окна
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like ($rootPat + "watch_izd2.ps1*") -and $_.ProcessId -ne $PID } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # сердцебиение прежней проверки распознаванием: процесс остановлен — сборка не должна его ждать
    Remove-Item "$Root\work\asr_active.txt" -Force -ErrorAction SilentlyContinue
} else { Write-Host "  -NoGpu: ничего не останавливаю (только подготовка)" }

$prepArgs = @("-Root", $Root, "-Src", $Src, "-Model", $Model, "-Accent", $Accent, "-MaxChars", $MaxChars)
# название книги — защита от чужой рукописи (оно должно быть в начале файла); у новой книги, пока название не вписано
# в таблицу настроек, там стоит имя папки — тогда проверки нет
if ($Title -and $Title -ne (Split-Path $Root -Leaf)) { $prepArgs += @("-Title", $Title) }
$docxCands = @()
if ($DocxSet) { $docxCands += [Environment]::ExpandEnvironmentVariables($DocxSet) }
if ($releaseInfo -and (Test-Path -LiteralPath "$Root\book.docx")) { $docxCands += "$Root\book.docx" }
if ($leafB -like "kniga2*") { $docxCands += @("$Inputs\kniga2.docx", "$Root\book.docx", "D:\kniga2\book.docx") }
elseif ($leafB -like "kniga3*") { $docxCands += @("$Inputs\kniga3.docx", "$Root\book.docx") }
elseif ($IsBook1) { $docxCands += @("$Inputs\kniga1.docx", "$Root\book.docx") }
else {
    # новая книга: её рукопись — book.docx в её папке (или <имя папки>.docx в Загрузках, или единственный .docx в папке)
    $docxCands += @("$Root\book.docx", "$Inputs\$leafB.docx")
    $one = @(Get-ChildItem "$Root\*.docx" -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "~*" })
    if ($one.Count -eq 1) { $docxCands += $one[0].FullName }
}
$docxPick = $docxCands | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if ($docxPick) { $prepArgs += @("-Docx", $docxPick) }
elseif (-not $IsBook1 -and $leafB -notlike "kniga2*" -and $leafB -notlike "kniga3*") {
    Write-Host ("Нет рукописи книги. Положите её как " + $Root + "\book.docx (или укажите путь в таблице настроек, строка «Рукопись») и запустите снова.") -ForegroundColor Red
    Write-Host "Ничего не изменено." -ForegroundColor Red
    exit 1
}
$ShareFrom = Cfg "share_from" ""
# несколько книг через «;» — роли и голоса для новой книги берутся из первой, где они уже есть
$SeedPick = "$ShareFrom".Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ -and (Test-Path "$_\work\roles.json") } | Select-Object -First 1
if ($SeedPick) { $prepArgs += @("-SeedFrom", $SeedPick) }
$dictIn = $Dict
if (-not $dictIn -and (Test-Path -LiteralPath "$Root\work\accent_dict.csv")) { $dictIn = "$Root\work\accent_dict.csv" }
if (-not $dictIn) { $dictIn = "$Inputs\accent_dict.csv" }
if ($Dict -and -not (Test-Path -LiteralPath $Dict)) { throw "Нет явно заданного словаря: $Dict" }
if (Test-Path -LiteralPath $dictIn) { $prepArgs += @("-Dict", $dictIn) }
if ($Pack) { $prepArgs += @("-FastPy", "$Pack\nox_fast.py") }
$stressIn = $StressFixes
if (-not $stressIn -and (Test-Path -LiteralPath "$Root\work\stress_fixes.tsv")) { $stressIn = "$Root\work\stress_fixes.tsv" }
if (-not $stressIn) { $stressIn = "$Inputs\stress_fixes_$leafB.tsv" }
if ($StressFixes -and -not (Test-Path -LiteralPath $StressFixes)) { throw "Нет явно заданного файла ударений: $StressFixes" }
if (Test-Path -LiteralPath $stressIn) { $prepArgs += @("-StressFixes", $stressIn) }
# Сценарий ролей ЭТОЙ книги — явно, без угадывания по дате: speaker_overrides_<книга>.tsv из Загрузок.
# Для книги 1 в нём должны быть исправления ролей от 05.11 (явная разметка «=», 115 абзацев) — иначе стоп.
$ovrFile = $Overrides
if (-not $ovrFile -and (Test-Path -LiteralPath "$Root\work\speaker_overrides.tsv")) { $ovrFile = "$Root\work\speaker_overrides.tsv" }
if (-not $ovrFile) { $ovrFile = "$Inputs\speaker_overrides_$leafB.tsv" }
if ($Overrides -and -not (Test-Path -LiteralPath $Overrides)) { throw "Нет явно заданного сценария ролей: $Overrides" }
if (Test-Path $ovrFile) {
    $nEq = @(Select-String -Path $ovrFile -Pattern "`t=`t" -SimpleMatch -Encoding UTF8).Count
    $nBang = @(Select-String -Path $ovrFile -Pattern "`t!`t" -SimpleMatch -Encoding UTF8).Count
    if ($leafB -eq "kniga1a" -and $nEq -lt 100) {
        Write-Host ("Сценарий ролей " + $ovrFile + " — СТАРЫЙ (нет исправлений ролей от 05.11).") -ForegroundColor Red
        Write-Host "Скачайте speaker_overrides_kniga1a.tsv из последнего сообщения Claude и запустите снова. Ничего не изменено." -ForegroundColor Red
        exit 1
    }
    $prepArgs += @("-Overrides", $ovrFile)
    Write-Host ("  сценарий ролей: " + $ovrFile + "  (явная разметка абзацев: " + $nEq + ", целиком голосом роли: " + $nBang + ")")
} elseif ($leafB -eq "kniga1a") {
    Write-Host ("Нет сценария ролей " + $ovrFile + " — скачайте его из последнего сообщения Claude. Ничего не изменено.") -ForegroundColor Red
    exit 1
}
# Подготовка (правки конвейера, словарь, сценарий ролей, разбивка рукописи) нужна, только когда изменился кто-то из её
# входов. Работа по частям — это десятки запусков go на книгу, и разбирать всю рукопись заново при каждом незачем.
function Sig-Of($paths, $extra) {
    $sb = New-Object System.Text.StringBuilder
    foreach ($f in $paths) {
        if ($f -and (Test-Path -LiteralPath $f)) { [void]$sb.Append((Get-FileHash -LiteralPath $f -Algorithm SHA1).Hash) } else { [void]$sb.Append("-") }
        [void]$sb.Append("|")
    }
    [void]$sb.Append($extra)
    $sha = [System.Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString())))).Replace("-", "")
}
$swFile = Get-Item -LiteralPath "$Root\work\stress_words.txt" -ErrorAction SilentlyContinue
if (-not $swFile) { $swFile = Get-ChildItem "$Inputs\stress_words*.txt" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1 }
$fastIn = if ($Pack) { "$Pack\nox_fast.py" } else { "$dl\nox_fast.py" }
$prepIn = @("$dl\prep_kniga1a.ps1", "$Src\nox_audiobook.py", "$Src\nox_fast.py", $docxPick, $ovrFile, $dictIn, $stressIn, $fastIn, $(if ($swFile) { $swFile.FullName } else { "" }))
$prepSig = Sig-Of $prepIn (($prepArgs -join "|") + "|" + $KIT)
$prepOld = ""
if (Test-Path "$Root\work\prep.sig") { $prepOld = (Get-Content "$Root\work\prep.sig" -Raw -ErrorAction SilentlyContinue) }
$prepReady = (Test-Path "$Root\work\segments.csv") -and (Test-Path "$Root\work\roles.json") -and (Test-Path "$Root\nox_audiobook.py") -and
             (Test-Path "$Root\envfast\Scripts\python.exe") -and (Test-Path "$Root\hf") -and (Select-String -Path "$Root\nox_audiobook.py" -SimpleMatch "NOX_STAGE = 21" -Quiet)
$PrepRan = $true
if ($prepReady -and $prepOld -and ($prepOld.Trim() -eq $prepSig)) {
    $PrepRan = $false
    Write-Host "=== Подготовка: рукопись, сценарий ролей, словарь и файлы комплекта не менялись — разбивка книги остаётся как есть" -ForegroundColor Cyan
    foreach ($f in @("nox_fast.py")) { if (Test-Path $fastIn) { Copy-Item $fastIn "$Root\$f" -Force } }
} else {
    Remove-Item "$Root\work\prep.sig" -Force -ErrorAction SilentlyContinue
    powershell -NoProfile -ExecutionPolicy Bypass -File "$dl\prep_kniga1a.ps1" @prepArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "Подготовка не прошла — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
    [IO.File]::WriteAllText("$Root\work\prep.sig", $prepSig, (New-Object System.Text.UTF8Encoding $false))
}
# A newly prepared book receives the same checked release record and code set.
# Existing prepared books were required to use the explicit updater above.
if ($Pack -and -not $releaseInfo) {
    & $PyFast -B (Join-Path $Pack 'nox_update.py') --root $Root --home $Src --bundle $packageRoot --output $Inputs --downloads $Inputs --apply
    if ($LASTEXITCODE -ne 0) { Write-Host "Новая книга подготовлена, но полный комплект не установлен; причина выше." -ForegroundColor Red; exit 1 }
    $releaseInfo = Get-Content -LiteralPath (Join-Path $Root 'work\nox_release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
# файлы работы по частям, самопроверки ударений и банка голосов — в папку книги
foreach ($f in @("nox_part.py", "nox_stress.py", "nox_voices.py", "zvuk_k1a.py", "nox_freeze.py", "nox_status.py", "nox_release_io.py", "nox_audio_archive.py", "nox_file_transactions.py", "nox_voice_safety.py")) { if (Test-Path "$dl\$f") { Copy-Item "$dl\$f" "$Root\$f" -Force } }

if ($ByParts) { Write-Host "=== Аудит слоя текста всей книги не нужен: записи текущей части сверяются перед её озвучкой (ниже)" -ForegroundColor Cyan }
else { Write-Host "=== Аудит слоя текста: что именно будет прочитано; переозвучка только изменившегося" -ForegroundColor Cyan }
$auditPy = @'
# -*- coding: utf-8 -*-
"""audit_k1a.py — аудит слоя текста (ruaccent + словарь + удвоение) и точечный сброс озвучки.
Запускается, когда TEXT_LAYER_VERSION в nox_audiobook.py отличается от work/text_layer.ver:
 - прогоняет prepare_text по всем сегментам -> work/audit_text.txt (id, роль, финальный текст, как его прочтёт движок);
 - work/audit_summary.txt: словарные замены, удвоенные слова, слова вне словаря ruaccent (кандидаты в словарь имён);
 - озвученный сегмент сбрасывается, если его финальный текст изменился (сравнение с work/wav/*.txt)
   или запись не прошла проверку дозаписанности: обрыв на громком звуке / слишком быстрый темп (выброшены слова) /
   «упёрлось в лимит» из suspicious.txt;
   wav без .txt (озвучен до появления аудита, прежним слоем текста) считается устаревшим всегда;
 - подписи незакреплённых частей сбрасываются; прежние аудиофайлы остаются до успешной пересборки.
Отчёт по сброшенным: work/audit_purged.txt. Если слой не менялся — ничего не делает."""
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import nox_audiobook as n  # noqa: E402

MODEL = sys.argv[2] if len(sys.argv) > 2 else "?"
ACCENT = sys.argv[3] if len(sys.argv) > 3 else "none"
# слой текста = версия кода + модель + режим ударений + модель омографов ruaccent + СОДЕРЖИМОЕ словаря автора:
# поправили ударение в accent_dict.csv -> переозвучиваются ровно те реплики, где текст для движка изменился
if ACCENT != "none":
    n.prepare_text("проверка", False, ACCENT)            # грузит ruaccent (имя его модели входит в слой)
import hashlib as _hl
try:
    _dsig = _hl.sha1(Path(n.WORD_DICT_PATH).read_bytes()).hexdigest()[:10]
except Exception:
    _dsig = "-"
try:                                                   # ударения по месту (work/stress_fixes.tsv)
    _dsig += "+" + _hl.sha1(Path(n.STRESS_FIX_PATH).read_bytes()).hexdigest()[:8]
except Exception:
    pass
cur = (f"{getattr(n, 'TEXT_LAYER_VERSION', 'v0')}|{MODEL}|{ACCENT}|{getattr(n, 'RUACC_MODEL', None) or '-'}"
       f"|dict:{_dsig}")
FORCE_QC = "--qc" in sys.argv                                          # проверка записей даже без смены слоя
ver_file = n.WORK / "text_layer.ver"
prev = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else ""
RECOMPUTE = prev != cur                                                # слой не менялся -> тексты не пересчитываем
if not RECOMPUTE and not FORCE_QC:
    print(f"  слой текста {cur} не менялся — аудит и сброс не нужны")
    sys.exit(0)

RE_L = re.compile(r"[^А-Яа-яЁё]")


def qc_wav(path, text):
    """Проверка дозаписанности готовой записи -> причина или None."""
    import numpy as np
    import soundfile as sf
    try:
        x, sr = sf.read(str(path), dtype="float32")
    except Exception as e:
        return f"не читается ({type(e).__name__})"
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) < sr * 0.15:
        return "пустая запись"
    k = int(sr * 0.08)
    body = float(np.sqrt(np.mean(np.square(x))))
    tail = float(np.sqrt(np.mean(np.square(x[-k:])))) if len(x) >= 4 * k else 0.0
    if body > 1e-4 and tail > 0.5 * body:
        return "обрыв в конце"
    cut = getattr(n, "cut_tail_db", None)
    if cut:
        tdb = cut(x, sr)
        if tdb > float((getattr(n, "SETTINGS", {}) or {}).get("tail_db", -16)):
            return f"обрублен конец слова ({tdb:.0f} дБ — запись кончается на звуке)"
    letters = len(RE_L.sub("", text))
    rate = letters / max(len(x) / sr, 0.05)
    if letters >= 60 and rate > 14.8:
        return f"слишком быстро ({rate:.1f} букв/с) — выброшены слова"
    return None


susp_ids = set()
sp = n.WORK / "suspicious.txt"
if sp.exists():
    for line in sp.read_text(encoding="utf-8").splitlines():
        if "лимит" in line:
            susp_ids.add(line.split("\t")[0].strip())
if RECOMPUTE:
    print(f"  слой текста: {prev or 'нет'} -> {cur}; прогоняю все сегменты (GPU не нужен)…", flush=True)
else:
    print("  слой текста не менялся; проверяю готовые записи на дозаписанность…", flush=True)
rows = n.load_rows()
roles = n.load_roles()
if hasattr(n, "migrate_voice_keys"):       # этап 18: имена записей с отпечатком образца голоса (разово, без потерь)
    n.migrate_voice_keys(rows, roles)
# этап 21: части, закреплённые автором в студии, аудит не трогает — ни их записи, ни их файлы
_fx = n.fixed_set(rows) if hasattr(n, "fixed_set") else set()
_nfx = len(n.fixed_prefix(rows)) if hasattr(n, "fixed_prefix") else 0
if _nfx:
    print(f"  закреплено автором частей: {_nfx} (реплик {len(_fx)}) — они не проверяются и не сбрасываются", flush=True)
# смена разбивки/сценария (кто говорит) -> другие имена wav у изменённых реплик; собранные части устарели
import hashlib
seg_sig = hashlib.sha1("\n".join(f"{r['id']}|{r['speaker']}|{r['text']}" for r in rows).encode("utf-8")).hexdigest()
seg_file = n.WORK / "segments.ver"
seg_prev = seg_file.read_text(encoding="utf-8").strip() if seg_file.exists() else ""
SEG_CHANGED = seg_prev != seg_sig
if SEG_CHANGED:
    print("  разбивка/сценарий по ролям изменились — собранные части будут пересобраны", flush=True)
if RECOMPUTE:
    n.prepare_text("проверка", False, ACCENT)
acc = getattr(n, "_ACCENTIZER", None)
d = n.load_word_dict()
rx_dict = (re.compile(r"(?<![А-Яа-яЁё])(" + "|".join(re.escape(w) for w in sorted(d, key=len, reverse=True))
                      + r")(?![А-Яа-яЁё])") if d else None)
dbl_cnt, dict_cnt = Counter(), Counter()
audit, purged = [], []
voiced = removed = 0
for k, r in enumerate(rows, 1):
    txt = r["text"]
    new_text = None
    if RECOMPUTE:
        new_text = n.prepare_text(txt, False, ACCENT)
        audit.append(f"{r['id']}\t{r['speaker']}\t{new_text}")
        if rx_dict:
            for m in rx_dict.finditer(txt):
                dict_cnt[m.group(1)] += 1
        for w in re.findall(r"[А-Яа-яЁё]+", new_text):
            lw = w.lower()
            if ("\u0301" in w or re.search(r"([аеёиоуыэюя])\1", lw)) and lw not in dict_cnt:
                dbl_cnt[lw] += 1
    p = n.wav_for(r, roles)
    if p.exists() and (k - 1) not in _fx:
        voiced += 1
        side = p.with_suffix(".txt")
        why = None
        if side.exists():
            if RECOMPUTE and side.read_text(encoding="utf-8") != new_text:
                why = "текст изменился"
            elif str(r["id"]) in susp_ids:
                why = "упёрлось в лимит длины"
            else:
                why = qc_wav(p, txt)
        else:
            why = "озвучен прежним слоем текста (нет .txt с прочитанным текстом)"
        if why:
            from nox_audio_archive import archive_stem
            def unchanged_unlocked():
                fresh = n.load_rows()
                if k > len(fresh) or fresh[k - 1] != r:
                    raise RuntimeError("разбивка изменилась во время аудита; прежнее аудио сохранено")
                return (k - 1) not in n.fixed_set(fresh)
            if archive_stem(n.WORK.parent, p.stem, why, target_row=dict(r), precondition=unchanged_unlocked) is None:
                continue
            removed += 1
            purged.append(f"{r['id']}\tгл.{r['chapter']}\t{why}\t{txt[:90]}")
    if k % 2000 == 0:
        print(f"  …{k}/{len(rows)}", flush=True)
if RECOMPUTE:
    (n.WORK / "audit_text.txt").write_text("\n".join(audit), encoding="utf-8")
neural = Counter(getattr(n, "_NEURAL_WORDS", {}) or {})
summ = [f"слой текста {cur}; сегментов {len(rows)}; озвучено было {voiced}, сброшено на переозвучку {removed}", "",
        f"== словарь произношения: {sum(dict_cnt.values())} замен, {len(dict_cnt)} форм (слово: сколько раз) =="]
summ += [f"{w}\t{c}" for w, c in dict_cnt.most_common()]
summ += ["", f"== слова с ударением от ruaccent (кроме словаря): {len(dbl_cnt)} форм =="]
summ += [f"{w}\t{c}" for w, c in dbl_cnt.most_common(300)]
summ += ["", f"== слова вне словаря ruaccent (ударение гадала нейросеть) — кандидаты в словарь имён: {len(neural)} =="]
summ += [f"{w}\t{c}" for w, c in neural.most_common(300)]
if RECOMPUTE:
    (n.WORK / "audit_summary.txt").write_text("\n".join(summ), encoding="utf-8")
(n.WORK / "audit_purged.txt").write_text("\n".join(purged), encoding="utf-8")
# wav, на которые больше не ссылается ни одна реплика (сменился говорящий/разбивка) — убрать, чтобы не копились
orphans = 0
if n.WAVS.is_dir():
    expected = {n.wav_for(r, roles).stem for r in rows}
    from nox_audio_archive import archive_stem
    obsolete = sorted({f.name.split(".")[0] for f in n.WAVS.iterdir()
                       if f.is_file() and f.suffix.lower() in (".wav", ".txt", ".ok", ".json", ".meta", ".gaps")
                       and f.name.split(".")[0] not in expected})
    for stem in obsolete:
        def still_orphan():
            fresh = n.load_rows()
            n.fixed_set(fresh)  # Invalid protection must stop the audit before a move.
            current_roles = n.load_roles()
            return stem not in {n.wav_for(row, current_roles).stem for row in fresh}
        if archive_stem(n.WORK.parent, stem, "прежняя разбивка или роль", precondition=still_orphan) is not None:
            orphans += 1
parts_invalidated = 0
if removed or SEG_CHANGED or orphans:
    import time
    from nox_file_transactions import directory_lock
    from nox_freeze import read_protection
    from nox_release_io import atomic_write
    with directory_lock(n.WORK.parent / "studio" / "transactions"):
        state = read_protection(n.WORK.parent, n.load_rows())
        fixed_numbers = {v[2] for v in state.ranges}
        if n.PARTS_INDEX.exists():
            old_parts = json.loads(n.PARTS_INDEX.read_text(encoding="utf-8-sig"))
            if not isinstance(old_parts, list):
                raise RuntimeError("неверный parts.json; готовые файлы сохранены")
            for part in old_parts:
                if int(part["n"]) not in fixed_numbers:
                    part["sig"] = None
                    parts_invalidated += 1
            atomic_write(n.PARTS_INDEX, json.dumps(old_parts, ensure_ascii=False, indent=1))
        # Duration is a derived cache. Keep its old bytes with the audit before invalidation.
        if n.DUR_CACHE.exists():
            history = n.WORK / "cache_history" / (str(time.time_ns()) + "_durations.json")
            atomic_write(history, n.DUR_CACHE.read_bytes())
            n.DUR_CACHE.unlink()
ver_file.write_text(cur, encoding="utf-8")
seg_file.write_text(seg_sig, encoding="utf-8")
if removed and sp.exists():
    sp.unlink()
print(f"  озвучено было {voiced}; на переозвучку {removed}; частей помечено для пересборки {parts_invalidated}; прежних записей сохранено в work/audio_history: {orphans}")
print(f"  словарь: {sum(dict_cnt.values())} замен; слов с ударением от ruaccent: {len(dbl_cnt)} форм; слов вне словаря ruaccent: {len(neural)}")
print("  отчёты: work\\audit_summary.txt (кратко), work\\audit_text.txt (весь текст как будет прочитан), work\\audit_purged.txt")
if RECOMPUTE and getattr(n, "STRESS_FIX_PATH", None) is not None and Path(n.STRESS_FIX_PATH).exists():
    _nf = sum(len(v) for v in n._load_stress_fixes().values())
    print(f"  ударения по месту: поправок в файле {_nf}; не подошло (текст реплики изменился) {getattr(n, 'SFIX_MISSED', 0)}")
'@
[IO.File]::WriteAllText("$Root\audit_k1a.py", $auditPy, (New-Object System.Text.UTF8Encoding $false))
$sfx = $StressFixes
if (-not $sfx -and (Test-Path -LiteralPath "$Root\work\stress_fixes.tsv")) { $sfx = "$Root\work\stress_fixes.tsv" }
if (-not $sfx) { $sfx = "$Inputs\stress_fixes_" + (Split-Path $Root -Leaf).ToLower() + ".tsv" }
if ($StressFixes -and -not (Test-Path -LiteralPath $StressFixes)) { throw "Нет явно заданного файла ударений: $StressFixes" }     # ударения по месту для ЭТОЙ книги
if (Test-Path $sfx) {
    $okBook = (Get-Content $sfx -Encoding UTF8 -TotalCount 1) -match ("^# book: " + (Split-Path $Root -Leaf).ToLower())
    if ($okBook) { if ([IO.Path]::GetFullPath($sfx) -ne [IO.Path]::GetFullPath("$Root\work\stress_fixes.tsv")) { Copy-Item $sfx "$Root\work\stress_fixes.tsv" -Force }; Write-Host ("  ударения по месту: " + $sfx) }
    else { Write-Host ("  " + $sfx + " — от другой книги, не беру") -ForegroundColor Yellow }
}
$env:HF_HOME = "$Root\hf"
if (-not $ByParts) {
    & $PyFast "$Root\audit_k1a.py" $Dest $Model $Accent --qc
    if ($LASTEXITCODE -ne 0) { Write-Host "аудит не выполнился — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
}

Write-Host "=== Кастинг голосов по возрасту/описанию персонажей" -ForegroundColor Cyan
$leaf = (Split-Path $Root -Leaf).ToLower()
$vfile = "voices_$leaf.py"                          # заготовки голосов этой книги (для книги 1 — voices_kniga1a.py)
if (Test-Path "$dl\$vfile") { Copy-Item "$dl\$vfile" "$Root\$vfile" -Force }
if (Test-Path "$Root\$vfile") {
    & "$Root\env\python.exe" "$Root\$vfile"
    if ($LASTEXITCODE -ne 0) { Write-Host "кастинг не применился — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
} else { Write-Host "  заготовок голосов $vfile нет — голоса по таблице настроек и автоописаниям" }
Write-Host "=== Роли из таблицы настроек (пол, возраст, описание, темп, громкость, «голос как у»)" -ForegroundColor Cyan
if (Test-Path "$Root\nastroyki_ozvuchki.py") {
    & $PyFast "$Root\nastroyki_ozvuchki.py" roles $Root
    if ($LASTEXITCODE -ne 0) { Write-Host "таблица ролей не применилась — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
}

if ($NoGpu) {
    Write-Host ("=== Подготовка без видеокарты завершена. Таблица: " + $Root + "\Настройки озвучки.xlsx") -ForegroundColor Green
    Write-Host "    Голоса и озвучка — обычным запуском go (без -NoGpu), когда видеокарта свободна." -ForegroundColor Green
    exit 0
}
# Резервная копия голосов и решений на Яндекс Диск: YandexDisk\nox_backup\<книга>. Ничего там не удаляет; если образец
# голоса изменился, прежняя копия не затирается, а уходит в voices_old с датой (любой прежний голос можно вернуть).
function Backup-Voices($when) {
    $ydisk = "$env:USERPROFILE\YandexDisk"
    if (-not (Test-Path $ydisk) -or -not (Test-Path "$Root\work\voices")) { return }
    $bk = Join-Path $ydisk ("nox_backup\" + (Split-Path $Root -Leaf))
    New-Item -ItemType Directory -Force -Path "$bk\voices", "$bk\voices_old" | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $new = 0; $kept = 0
    foreach ($f in Get-ChildItem "$Root\work\voices" -File -ErrorAction SilentlyContinue) {
        $dst = Join-Path "$bk\voices" $f.Name
        if (Test-Path $dst) {
            $d = Get-Item $dst
            if ($d.Length -eq $f.Length -and $d.LastWriteTimeUtc -eq $f.LastWriteTimeUtc) { continue }
            Move-Item $dst (Join-Path "$bk\voices_old" ($f.BaseName + "__" + $stamp + $f.Extension)) -Force -ErrorAction SilentlyContinue
            $kept++
        }
        Copy-Item $f.FullName $dst -Force -ErrorAction SilentlyContinue
        $new++
    }
    foreach ($x in @("roles.json", "accent_dict.csv", "settings.json", "speaker_overrides.tsv", "stress_fixes.tsv")) {
        if (Test-Path "$Root\work\$x") { Copy-Item "$Root\work\$x" "$bk\$x" -Force -ErrorAction SilentlyContinue }
    }
    Copy-Item "$Root\Настройки озвучки.xlsx" "$bk\" -Force -ErrorAction SilentlyContinue
    $nv = (Get-ChildItem "$bk\voices" -Filter *.wav -ErrorAction SilentlyContinue).Count
    Write-Host ("  резервная копия (" + $when + "): голосов " + $nv + ", обновлено " + $new + ", прежних версий сохранено " + $kept + " -> " + $bk)
}

Backup-Voices "до создания и проверки голосов"
Set-Location $Root
$HasVoices = Test-Path "$Root\nox_voices.py"
# режим работы — до подбора голосов: по нему видно, какие роли нужны текущей части
if (Test-Path "$Root\nox_part.py") {
    $modeArgs = @("--root", $Root, "--mode") + $partArgs
    if ($All) { $modeArgs += "--all" }
    & $PyFast "$Root\nox_part.py" @modeArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "режим работы не записан (nox_part.py) — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
}
function Make-Voices {
    # голоса ролям без образца: описанным автором — по описанию, остальным — из банка голосов (нет подходящего — создать)
    if ($HasVoices) {
        $va = @("$Root\nox_voices.py", "prepare", "--root", $Root)
        if ($All) { $va += "--all" }
        & "$Root\env\python.exe" @va
        if ($LASTEXITCODE -ne 0) { Write-Host "подбор голосов не выполнился — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
    }
    if ($All -or -not $HasVoices) {
        & "$Root\env\python.exe" "$Root\nox_audiobook.py" cast --attn sdpa
        if ($LASTEXITCODE -ne 0) { Write-Host "cast упал — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
    }
}
if ($HasVoices) {
    # голоса этой книги — в общий банк голосов (копии; пригодятся следующим книгам)
    & $PyFast "$Root\nox_voices.py" harvest --root $Root --quiet
}
if ($ByParts) { Write-Host "=== Голоса ролям текущей части (банк голосов и подбор; у кого образец есть — не трогаются)" -ForegroundColor Cyan }
else { Write-Host "=== Создание голосов всем ролям книги без образца (банк голосов; по описаниям — ~10 секунд на голос)" -ForegroundColor Cyan }
Make-Voices

Write-Host "=== Проверка образцов голосов по высоте тона: новый голос не того пола пересоздаётся; голос, которым уже озвучено, сам не трогается" -ForegroundColor Cyan
if (Test-Path "$dl\voicecheck_k1a.py") { Copy-Item "$dl\voicecheck_k1a.py" "$Root\voicecheck_k1a.py" -Force }
if (Test-Path "$Root\voicecheck_k1a.py") {
    for ($round = 1; $round -le 2; $round++) {      # не больше 2 кругов за запуск (каждый круг — минуты); дальше решает автор
        & $PyFast "$Root\voicecheck_k1a.py"
        $recast = @()
        if (Test-Path "$Root\work\recast.txt") { $recast = Get-Content "$Root\work\recast.txt" -Encoding UTF8 | Where-Object { $_.Trim() } }
        if ($recast.Count -eq 0) { break }
        Write-Host ("  пересоздаю " + $recast.Count + " голос(ов), круг " + $round) -ForegroundColor Cyan
        Make-Voices
        if (Test-Path "$Root\work\recast.txt") { Set-Content "$Root\work\recast.txt" "" -Encoding UTF8 }
    }
} else { Write-Host "  voicecheck_k1a.py не найден — проверка пропущена" -ForegroundColor Yellow }
if (Test-Path "$Root\nastroyki_ozvuchki.py") { & $PyFast "$Root\nastroyki_ozvuchki.py" roles $Root | Out-Null }
Write-Host ("  таблица настроек: " + $Root + "\Настройки озвучки.xlsx  (листы «Общие», «Роли», «Как пользоваться»)") -ForegroundColor Green
Backup-Voices "после проверки голосов"
if ($PrepRan -or $All -or $Probe -or -not (Test-Path "$Root\Разметка_книги.txt")) {
    Write-Host "=== Разметка книги тегами, карта ролей «кто за кем», отчёт о голосах" -ForegroundColor Cyan
    Run-Map
} else { Write-Host "=== Разметка книги и карта ролей — прежние (книга не менялась); освежить: go -Map" -ForegroundColor Cyan }
if ($Probe) {
    # опыт и сверка ничего в книге не меняют; озвучка не запускается
    foreach ($f in @("probe_k1a.py", "stress_check.py", "nox_fast.py")) { if (Test-Path "$dl\$f") { Copy-Item "$dl\$f" "$Root\$f" -Force } }
    Write-Host "=== Сверка ударений двумя независимыми моделями (ruaccent + Silero; процессор, 5-15 минут)" -ForegroundColor Cyan
    if (Test-Path "$Root\stress_check.py") { & $PyFast "$Root\stress_check.py" $Root } else { Write-Host "  stress_check.py нет в Загрузках — пропущено" -ForegroundColor Yellow }
    Write-Host "=== Опыт на видеокарте: 6 способов озвучки на трудных репликах (~30-40 минут)" -ForegroundColor Cyan
    if (Test-Path "$Root\probe_k1a.py") { & $PyFast "$Root\probe_k1a.py" $Root } else { Write-Host "  probe_k1a.py нет в Загрузках — пропущено" -ForegroundColor Yellow }
    Write-Host ("=== Готово. Пришлите в чат два файла: " + $Root + "\probe\report.txt  и  " + $Root + "\work\stress_check.txt") -ForegroundColor Green
    if (Test-Path "$Root\probe") { Start-Process explorer.exe "$Root\probe" }
    exit 0
}
if ($PrepOnly) { Write-Host "=== Подготовка завершена (озвучка не запускалась)" -ForegroundColor Green; exit 0 }

function After-Part($num) {
    # часть собрана: самопроверка ударений второй моделью и студия — прослушать, поправить, закрепить
    foreach ($f in @("nox_stress.py")) { if (Test-Path "$dl\$f") { Copy-Item "$dl\$f" "$Root\$f" -Force } }
    if (Test-Path "$Root\nox_stress.py") {
        Write-Host ("=== Самопроверка ударений части " + $num + ": вторая, независимая модель ударений (процессор, до минуты)") -ForegroundColor Cyan
        $env:HF_HOME = "$Root\hf"
        & $PyFast "$Root\nox_stress.py" --root $Root --part $num --accent $Accent
        if ($LASTEXITCODE -ne 0) { Write-Host "  самопроверка ударений не выполнилась (озвучке это не мешает) — пришлите вывод в чат" -ForegroundColor Yellow }
    }
    if (Test-Path "$dl\nox_studio.ps1") {
        if ($NoStudio) { powershell -NoProfile -ExecutionPolicy Bypass -File "$dl\nox_studio.ps1" -Root $Root -NoxHome $Src -Pack $dl -NoStart | Out-Null }
        else {
            Write-Host "=== Открываю «Нокс Студию»: прослушайте часть, поправьте, что нужно, и закрепите её" -ForegroundColor Cyan
            powershell -NoProfile -ExecutionPolicy Bypass -File "$dl\nox_studio.ps1" -Root $Root -NoxHome $Src -Pack $dl
        }
    }
    try { [console]::beep(784, 180); [console]::beep(988, 260) } catch {}    # часть готова — короткий сигнал
    Write-Host ("=== ЧАСТЬ " + $num + " ГОТОВА К ПРОСЛУШИВАНИЮ. Закрепите её в студии — и запустите go снова (или кнопка «Озвучить следующую часть» в студии): пойдёт часть " + ($num + 1) + ".") -ForegroundColor Green
}
$pst = $null
if ($ByParts) {
    Write-Host "=== Текущая часть: сверка её записей с нынешним текстом (словарь автора, ударения по месту)" -ForegroundColor Cyan
    $env:HF_HOME = "$Root\hf"
    $ia = @("--root", $Root, "--init", "--accent", $Accent, "--dest", $Dest) + $partArgs
    & $PyFast "$Root\nox_part.py" @ia
    if ($LASTEXITCODE -ne 0) { Write-Host "режим «по частям» не запустился (nox_part.py) — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
    $pst = Part-Status
    if ($pst -and $pst.done_all) {
        Write-Host "=== ВСЕ ЧАСТИ КНИГИ ЗАКРЕПЛЕНЫ — озвучивать больше нечего. Готовые части и главы — в студии (кнопка «Готовые части»)." -ForegroundColor Green
        exit 0
    }
    if ($pst -and $pst.closed) {
        Write-Host ("  часть " + $pst.part + " уже собрана из нынешних записей — озвучка не нужна") -ForegroundColor Green
        After-Part ([int]$pst.part)
        exit 0
    }
}
$AsrOn = ((Cfg "asr_check" $true) -ne $false)
$VerOn = $false
Remove-Item "$Root\work\asr_okv.txt" -Force -ErrorAction SilentlyContinue   # новая проверка запишет свою метку
if (Test-Path "$dl\verify_k1a.py") { Copy-Item "$dl\verify_k1a.py" "$Root\verify_k1a.py" -Force }
if (Test-Path "$dl\nox_fast.py") { Copy-Item "$dl\nox_fast.py" "$Root\nox_fast.py" -Force }   # проверка голоса берёт функции оттуда
if (Test-Path "$dl\diag_k1a.py") { Copy-Item "$dl\diag_k1a.py" "$Root\diag_k1a.py" -Force }
if ($AsrOn -and (Test-Path "$Root\verify_k1a.py")) {
    Write-Host "=== Проверка каждой реплики распознаванием речи (Whisper на процессоре; недочитанное переозвучивается)" -ForegroundColor Cyan
    & $PyFast -c "import faster_whisper" 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($releaseInfo) { throw "В рабочем envfast не импортируется faster_whisper, а проверка речи включена. Пакеты не менялись; озвучка не запущена без обязательной проверки." }
        Write-Host "  ставлю распознаватель faster-whisper в envfast, разово (уже стоящие пакеты не меняются)"
        $cons = "$Root\work\pip_constraints.txt"
        & $PyFast -m pip freeze 2>$null | Where-Object { $_ -match '^[A-Za-z0-9_.\-]+==' } | Set-Content $cons -Encoding ASCII
        & $PyFast -m pip install -q faster-whisper -c $cons
        & $PyFast -c "import faster_whisper, transformers, faster_qwen3_tts" 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Host "  распознаватель не встал — озвучка пойдёт без этой проверки (как раньше)" -ForegroundColor Yellow }
    }
    & $PyFast -c "import faster_whisper" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $env:HF_HOME = "$Root\hf"
        $vp = Start-Process -FilePath $PyFast -ArgumentList @("$Root\verify_k1a.py") -WorkingDirectory $Root -WindowStyle Hidden `
                      -RedirectStandardOutput "$Root\verify.log" -RedirectStandardError "$Root\verify.err.log" -PassThru
        Write-Host ("  запущена скрыто; ход проверки: " + $Root + "\verify.log, отбраковка: " + $Root + "\work\asr_log.txt")
        # окно сборки частей стартует ТОЛЬКО после того, как проверка объявилась (сердцебиение + метка asr_okv.txt):
        # раньше первый круг сборки успевал пройти «без проверки» и собирал части из непроверенных записей
        $t0v = Get-Date
        while (((Get-Date) - $t0v).TotalSeconds -lt 180) {
            if ((Test-Path "$Root\work\asr_active.txt") -and (Test-Path "$Root\work\asr_okv.txt")) { $VerOn = $true; break }
            if ($vp.HasExited) { break }
            Start-Sleep 2
        }
        if (-not $VerOn) {
            if ($vp.HasExited) {
                # успела проверить всё и выйти (метка осталась) — проверка в силе; метки нет — распознаватель не встал
                $VerOn = (Test-Path "$Root\work\asr_okv.txt")
            } elseif ((Test-Path "$Root\work\asr_active.txt") -and (Test-Path "$Root\work\asr_okv.txt")) {
                $VerOn = $true
            } else {
                # не объявилась за 3 минуты — не оставлять работающую проверку без метки (сборка взяла бы старые отметки)
                Stop-Process -Id $vp.Id -Force -ErrorAction SilentlyContinue
            }
        }
        if ($VerOn) { Write-Host ("  проверка объявилась (метка " + (Get-Content "$Root\work\asr_okv.txt" -Raw).Trim() + "): части собираются только из проверенных ею записей") }
        else { Write-Host ("  проверка не запустилась — части пойдут без неё; причина в " + $Root + "\verify.log и verify.err.log") -ForegroundColor Yellow }
    }
} else { Write-Host "  проверка распознаванием выключена в таблице настроек" }
# проверки нет — сборка не должна ждать её метку (иначе части не собирались бы вовсе)
if (-not $VerOn) { Remove-Item "$Root\work\asr_okv.txt" -Force -ErrorAction SilentlyContinue }

$partProc = $null
if ($ByParts) {
    Write-Host ("=== Сборка текущей части (скрыто): ход — " + $Root + "\part.log; черновик на Диске — " + $Dest) -ForegroundColor Cyan
    $pa = @('"' + "$Root\nox_part.py" + '"', "--root", ('"' + $Root + '"'), "--dest", ('"' + $Dest + '"')) + $partArgs
    $partProc = Start-Process -FilePath $PyFast -ArgumentList $pa -WorkingDirectory $Root -WindowStyle Hidden `
                    -RedirectStandardOutput "$Root\part.log" -RedirectStandardError "$Root\part.err.log" -PassThru
} else {
Write-Host ("=== Окно отправки частей на Диск: " + $Dest) -ForegroundColor Cyan
Write-Host ("  у этого запуска своя папка (" + $RunStamp + "); уже собранные части тоже копируются в неё; прежние папки не трогаются")
$ws = "$Root\watch_izd2.ps1"
$watch = "chcp 65001|Out-Null`r`n" +
         "`$env:PYTHONUTF8='1'; `$env:PYTHONIOENCODING='utf-8'`r`n" +
         "Set-Location '$Root'`r`n" +
         "& '$Root\env\python.exe' '$Root\nox_audiobook.py' watch --parts $PartMin --dest `"$Dest`" --format $Fmt --bitrate $Bitrate --title `"$Title`" --author `"$Author`" --tempo $TempoStr --pauses $PausesStr$TrimArg$EqArg`r`n"
[IO.File]::WriteAllText($ws, $watch, (New-Object System.Text.UTF8Encoding $true))
$twatch = Get-Date
Start-Process powershell -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", $ws)
# звуковой слой стартует после того, как окно сборки сверило отпечаток сборки (тогда же идёт разовый сброс прежних
# частей): иначе сведение успело бы взять прежние части и положить их сведения в папку этого запуска
$t0w = Get-Date
while (((Get-Date) - $t0w).TotalSeconds -lt 90) {
    $af = Get-Item "$Root\work\assembly.json" -ErrorAction SilentlyContinue
    if ($af -and $af.LastWriteTime -ge $twatch.AddSeconds(-2)) { break }
    Start-Sleep 2
}

# Звуковой слой: эффекты, фон местности, музыка — сводятся с готовыми частями по мере их сборки (голос не меняется).
# Варианты: голос+эффекты, голос+музыка, всё вместе -> D:\kniga1a\parts_zvuk и на Диск в папку этого запуска.
if (Test-Path "$dl\zvuk_k1a.py") { Copy-Item "$dl\zvuk_k1a.py" "$Root\zvuk_k1a.py" -Force }
$zkit = (Test-Path "D:\nox_audiobook\zvuk") -or (@(Get-ChildItem "$dl\Nox_*.zip" -ErrorAction SilentlyContinue).Count -gt 0)
if ((Test-Path "$Root\zvuk_k1a.py") -and $zkit) {
    Write-Host "=== Звуковой слой: эффекты, фон, музыка — сводятся с частями по мере сборки (скрыто)" -ForegroundColor Cyan
    $zArgs = '"' + "$Root\zvuk_k1a.py" + '" --root "' + $Root + '" --dest "' + $Dest + '" --watch'
    Start-Process -FilePath $PyFast -ArgumentList $zArgs -WorkingDirectory $Root -WindowStyle Hidden `
                  -RedirectStandardOutput "$Root\zvuk.log" -RedirectStandardError "$Root\zvuk.err.log" | Out-Null
    Write-Host ("  ход сведения: " + $Root + "\zvuk.log; варианты на Диске: " + $Dest + "\effekty, \muzyka, \vse")
} else { Write-Host "  звуковой комплект (Nox_…zip в Загрузках) не найден — звуковой слой не сводится" -ForegroundColor Yellow }
}   # конец ветки -All (окно watch и звуковой слой); по частям звук и музыку сводит студия при закреплении части

# Редактор ручной правки «Нокс Студия»: ставится/обновляется и получает ярлык; запускается ярлыком, когда собрана часть.
# Воркеры озвучки сами обслуживают его заявки на переозвучку фраз — отдельная модель в видеопамяти не нужна.
if (Test-Path "$dl\nox_studio.ps1") {
    Write-Host "=== Редактор ручной правки «Нокс Студия»" -ForegroundColor Cyan
    powershell -NoProfile -ExecutionPolicy Bypass -File "$dl\nox_studio.ps1" -Root $Root -NoxHome $Src -Pack $dl -NoStart
    if ($LASTEXITCODE -ne 0) { Write-Host "Комплект Studio не прошёл проверку; озвучка не запущена." -ForegroundColor Red; exit 1 }
    if (-not $ByParts) { Write-Host "  когда соберётся первая часть — ярлык «Nox Studio» на рабочем столе: правка, переозвучка фраз, закрепление" }
}

Write-Host ("=== Озвучка (быстрый движок, воркеров: " + $Workers + ")") -ForegroundColor Cyan
$rfArgs = @("-Root", $Root, "-NoxHome", $Src, "-Workers", "$Workers", "-Model", $Model, "-Accent", $Accent)
if ($Pack) { $rfArgs += @("-FastPy", "$Pack\nox_fast.py") }
if ($ByParts) { $rfArgs += "-ByParts" }
powershell -NoProfile -ExecutionPolicy Bypass -File "$dl\run_fast.ps1" @rfArgs
$workersExit = $LASTEXITCODE
if ($workersExit -ne 0) { Write-Host "Воркеры сообщили об ошибке; готовые записи сохранены, смотрите fast_w*.log и log_fast.txt." -ForegroundColor Red }
if ($ByParts) {
    # воркеры вышли: часть либо собрана, либо сборщик её дособирает (ждёт последние отметки проверки) — дождаться
    if ($partProc -and -not $partProc.HasExited) {
        Write-Host "=== Воркеры озвучки закончили; жду сборку части (не дольше 20 минут)" -ForegroundColor Cyan
        $partProc | Wait-Process -Timeout 1200 -ErrorAction SilentlyContinue
    }
    if (Test-Path "$Root\part.log") { Get-Content "$Root\part.log" -Encoding UTF8 -Tail 4 | ForEach-Object { Write-Host ("  " + $_) } }
    $pst = Part-Status
    if ($pst -and $pst.closed) { After-Part ([int]$pst.part) }
    elseif ($pst -and $pst.done_all) { Write-Host "=== Все части книги закреплены." -ForegroundColor Green }
    else {
        Write-Host ("=== Часть " + $(if ($pst) { $pst.part } else { "?" }) + " пока не собрана: озвучено " + $(if ($pst) { "" + $pst.have + " из " + $pst.total } else { "?" }) + " реплик. Причина — в " + $Root + "\part.log, work\failed.txt, work\asr_log.txt. Запустите go ещё раз.") -ForegroundColor Yellow
    }
}

if ($workersExit -ne 0) { exit $workersExit }
