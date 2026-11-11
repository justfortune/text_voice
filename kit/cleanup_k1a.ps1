# cleanup_k1a.ps1 — ПОЛНАЯ ЗАЧИСТКА книги 1 перед свежей озвучкой с нуля.
# NOX_KIT 2026-11-12a
#  1) останавливает всё, что работает по этой книге (воркеры озвучки, проверку распознаванием, окна отправки на Диск);
#  2) в D:\kniga1a удаляет ВСЮ озвучку (work\wav), собранные части, все кэши, отметки, отчёты, логи и служебные копии скриптов;
#  3) на Яндекс Диске НИЧЕГО не удаляет: каждый запуск go пишет в свою папку с датой и временем (третье издание книги 1 —
#     Posoh_izdanie3\<дата_время>); прежние запуски и второе издание остаются, лишнее удаляйте сами;
#  4) в Загрузках из каждого семейства файлов оставляет самый новый под правильным именем (без « (1)»),
#     старые копии переносит в Загрузки\_nox_old (ничего не удаляет).
# ОСТАЁТСЯ: голоса персонажей (work\voices и прежние версии в work\voices_old), роли (work\roles.json), таблица «Настройки
#   озвучки.xlsx», рукопись book.docx, словарь, ударения по месту (stress_fixes.tsv), сценарий ролей, учёт проверки голосов,
#   прежняя карта ролей (role_map.tsv — после зачистки видно, что изменилось), окружения и модели (env, envfast, hf).
#   Вторая книга и D:\nox_audiobook не трогаются.
# Запуск:  powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\cleanup_k1a.ps1"
param([string]$Root = "D:\kniga1a")
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$dl   = "$env:USERPROFILE\Downloads"
$leaf = (Split-Path $Root -Leaf).ToLower()
$other = if ($leaf -eq "kniga1a") { "kniga2a" } else { "kniga1a" }

# папка на Диске — как в go: из таблицы настроек (work\settings.json), иначе по умолчанию
$DestName = if ($leaf -eq "kniga1a") { "Posoh_izdanie3" } else { "Imya_goroda" }
if (Test-Path "$Root\work\settings.json") {
    try {
        $st = Get-Content "$Root\work\settings.json" -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        if ($st.dest) { $DestName = "$($st.dest)" }
    } catch {}
}
if ($DestName -match '^[A-Za-z]:\\') { $disk = $DestName } else { $disk = "$env:USERPROFILE\YandexDisk\$DestName" }

Write-Host "=== 1. Останавливаю озвучку, проверку и отправку на Диск книги $Root" -ForegroundColor Cyan
$n = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$leaf*" -and $_.CommandLine -notlike "*$other*" -and
                   ($_.CommandLine -like "*nox_fast*" -or $_.CommandLine -like "*nox_audiobook.py*" -or
                    $_.CommandLine -like "*verify_k1a*" -or $_.CommandLine -like "*nox_probe*" -or
                    $_.CommandLine -like "*voicecheck_k1a*" -or $_.CommandLine -like "*audit_k1a*" -or
                    $_.CommandLine -like "*razmetka_k1a*" -or $_.CommandLine -like "*zvuk_k1a*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*$leaf*" -and $_.CommandLine -notlike "*$other*" -and
                   ($_.CommandLine -like "*watch_izd2.ps1*" -or $_.CommandLine -like "*run_fast*" -or
                    $_.CommandLine -like "*go_kniga1a*" -or $_.CommandLine -like "*prep_kniga1a*") -and
                   $_.CommandLine -notlike "*cleanup_k1a*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
Start-Sleep 3                                   # Windows отпускает файлы не сразу
Write-Host ("  остановлено процессов: " + $n)

$script:freed = 0
$script:files = 0
function Remove-Quiet($path) {
    $items = @(Get-ChildItem -Path (Split-Path $path) -Filter (Split-Path $path -Leaf) -Force -ErrorAction SilentlyContinue)
    if ($items.Count -eq 0) { return }
    $sz = 0; $cnt = 0
    foreach ($it in $items) {
        if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }   # ссылки на окружения — никогда
        if ($it.PSIsContainer) {
            $inner = @(Get-ChildItem -LiteralPath $it.FullName -Recurse -File -Force -ErrorAction SilentlyContinue)
            $sz += ($inner | Measure-Object Length -Sum).Sum; $cnt += $inner.Count
        } else { $sz += $it.Length; $cnt += 1 }
        Remove-Item -LiteralPath $it.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    $script:freed += $sz; $script:files += $cnt
    Write-Host ("  - " + $path + "  (" + $cnt + " файл., " + [math]::Round($sz / 1MB, 1) + " МБ)")
}

Write-Host "=== 2. Удаляю озвучку, части, кэши и служебное в $Root" -ForegroundColor Cyan
if (Test-Path "$Root\work") {
    # в work остаётся только это; всё остальное — производное и создаётся заново
    $keep = @("voices", "voices_old", "roles.json", "settings.json", "accent_dict.csv", "speaker_overrides.tsv", "stress_words.txt",
              "stress_fixes.tsv", "voicecheck_tries.json", "voicecheck.json", "voicecheck.txt", "role_map.tsv")
    Get-ChildItem -LiteralPath "$Root\work" -Force -ErrorAction SilentlyContinue |
        Where-Object { $keep -notcontains $_.Name } |
        ForEach-Object { Remove-Quiet ("$Root\work\" + $_.Name) }
}
if (Test-Path $Root) {
    foreach ($m in @("parts", "sample", "__pycache__", "book.mp3", "book.m4b", "book.*.tmp*", "*.wav.tmp",
                     "fast_w*.log", "log_fast.txt", "verify.log", "verify.err.log", "watch_izd2.ps1",
                     "nox_audiobook.py", "nox_audiobook.py.bak*", "patch_k1a.py", "audit_k1a.py", "purge_k1a.py",
                     "Разметка_книги.txt", "probe", "parts_zvuk", "zvuk.log", "zvuk.err.log", "zvuk_k1a.py",
                     "razmetka_k1a.py", "nox_fast.py", "verify_k1a.py", "diag_k1a.py",
                     "nox_part.py", "nox_stress.py", "nox_voices.py", "part.log", "part.err.log", "studio_go.log")) {
        Remove-Quiet "$Root\$m"
    }
    Write-Host ("  голосов персонажей сохранено: " + @(Get-ChildItem "$Root\work\voices\*.wav" -ErrorAction SilentlyContinue).Count)
    Write-Host "  сохранены: роли, таблица «Настройки озвучки.xlsx», рукопись, словарь, сценарий ролей, окружения и модели"
    if ((Test-Path "$Root\studio") -or (Test-Path "$Root\gotovo")) {
        Write-Host "  НЕ ТРОНУТЫ: ручные правки и дубли «Нокс Студии» ($Root\studio), готовые части и главы ($Root\gotovo)" -ForegroundColor Green
        Write-Host "  Части, закреплённые в студии, остаются закреплёнными: go продолжит с первой незакреплённой части." -ForegroundColor Green
    }
} else { Write-Host "  папки $Root нет — пропускаю" }

if ($leaf -eq "kniga1a" -and $DestName -eq "Posoh_izdanie2") { $DestName = "Posoh_izdanie3"; $disk = "$env:USERPROFILE\YandexDisk\$DestName" }
Write-Host "=== 3. Яндекс Диск: $disk — ничего не удаляю" -ForegroundColor Cyan
Write-Host "  каждый запуск go пишет части в свою папку внутри, с датой и временем начала запуска; прежние остаются"
if (Test-Path $disk) {
    $runs = @(Get-ChildItem -LiteralPath $disk -Directory -ErrorAction SilentlyContinue | Sort-Object Name)
    foreach ($d in $runs) {
        $sz = (Get-ChildItem -LiteralPath $d.FullName -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        Write-Host ("  " + $d.Name + "  (" + [math]::Round($sz / 1MB) + " МБ)")
    }
}

Write-Host "=== 4. Порядок в Загрузках: самое новое — под правильным именем, старые копии -> _nox_old" -ForegroundColor Cyan
$old = Join-Path $dl "_nox_old"
New-Item -ItemType Directory -Path $old -Force | Out-Null
$patterns = @("go_kniga1a*.ps1", "go_kniga1a*.bat", "prep_kniga1a*.ps1", "prep_kniga2*.ps1", "prep_kniga2*.bat",
              "run_fast*.ps1", "run_fast*.bat", "nox_fast*.py", "accent_dict*.csv", "nox_probe*.py", "run_probe*.ps1",
              "collect_for_claude*.ps1", "nox_patch*.py", "run_all*.ps1", "run_all*.bat", "watch_v*.ps1", "watch_v*.bat",
              "NOX_FIX_*.zip", "Nox_Audiobook_Project*.zip", "Downloads*.zip", "claude-transfer-*.zip", "sox-14.4.2-win32*.zip",
              "stress_words*.txt", "sample_k1a*.ps1", "nox_sample*.py", "voices_*.py", "cleanup_k1a*.ps1", "cleanup_k1a*.bat",
              "voicecheck_k1a*.py", "speaker_overrides*.tsv", "scenariy_*.zip", "nastroyki_ozvuchki*.py", "verify_k1a*.py", "diag_k1a*.py",
              "primer_list_*.png", "probe_k1a*.py", "stress_check*.py", "stress_fixes*.tsv", "razmetka_k1a*.py", "zvuk_k1a*.py",
              "Nox_*.zip")
$moved = 0; $renamed = 0
foreach ($pat in $patterns) {
  # семейство = одно имя без « (N)»: speaker_overrides_kniga1a.tsv и speaker_overrides_kniga2a.tsv — разные семейства
  $groups = Get-ChildItem -Path $dl -Filter $pat -File -ErrorAction SilentlyContinue |
            Group-Object { ($_.Name -replace ' \(\d+\)(?=\.[^.]+$)', '') }
  foreach ($g in $groups) {
    $fam = @($g.Group | Sort-Object LastWriteTime -Descending)
    $newest = $fam[0]
    $fam | Select-Object -Skip 1 | ForEach-Object {
        $dest = Join-Path $old $_.Name
        if (Test-Path $dest) { $dest = Join-Path $old ($_.BaseName + "_" + $_.LastWriteTime.ToString("MMdd_HHmmss") + $_.Extension) }
        Move-Item -LiteralPath $_.FullName -Destination $dest -Force -ErrorAction SilentlyContinue
        $moved++
    }
    if ($newest.Name -ne $g.Name) {
        Move-Item -LiteralPath $newest.FullName -Destination (Join-Path $dl $g.Name) -Force -ErrorAction SilentlyContinue
        Write-Host ("  " + $newest.Name + " -> " + $g.Name)
        $renamed++
    }
  }
}
if (Test-Path "$dl\__pycache__") { Remove-Item "$dl\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host ("  переименовано в правильные имена: " + $renamed + "; старых копий перенесено: " + $moved + " -> " + $old)

Write-Host ""
Write-Host ("ЗАЧИСТКА ЗАВЕРШЕНА: удалено " + $script:files + " файлов, освобождено " + [math]::Round($script:freed / 1GB, 2) + " ГБ.") -ForegroundColor Green
Write-Host "Дальше — обычный запуск go: подготовка, голоса и озвучка с нуля; части появятся на Диске по мере проверки." -ForegroundColor Green
