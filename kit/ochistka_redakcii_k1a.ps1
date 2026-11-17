# ochistka_redakcii_k1a.ps1 — NOX_KIT 2026-11-17a. ЗАЧИСТКА СТАРОЙ РЕДАКЦИИ книги перед озвучкой новой (текст изменился, тайминги другие).
#
# Что делает:
#  1) останавливает всё, что работает по книге (озвучка, проверка, студия, разметка);
#  2) БЕЗ ключа -NoBackup: сначала кладёт копию всего малого и важного в D:\<книга>_arhiv_<дата_время>:
#       правки студии (edits.json: ручные правки, дубли, закрепления, вставки), всё небольшое из work (разбивка, роли, отчёты),
#       образцы голосов персонажей (work\voices). Звук озвучки, части и готовые главы НЕ копируются — они уже на Яндекс Диске;
#  3) удаляет озвучку старого текста (work\wav и производное), собранные части, готовые главы (gotovo), записи студии
#     (takes, cache, render, trash), закрепления и правки студии (edits.json, fixed_parts.json) — студия начнёт с чистого листа
#     и больше не ссылается на фразы и тайминги старой книги.
# ОСТАЁТСЯ: образцы голосов и роли, таблица «Настройки озвучки.xlsx», рукопись book.docx, словарь, ударения по месту,
#   сценарий ролей, окружения и модели; библиотека звуков студии (studio\sounds, imports, projects); партитура музыки/эффектов/
#   фона (cues_book1.json в D:\nox_audiobook\zvuk) — отметки ищут свои фразы по тексту в новой книге заново (главы
#   перенумерованы — это учитывается), новых сцен они дополняются в студии («Звуки и музыка»).
# Яндекс Диск и D:\nox_audiobook НЕ ТРОГАЮТСЯ. Вторая книга не трогается.
#
# Запуск С копией (рекомендуется):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\ochistka_redakcii_k1a.ps1"
# Запуск БЕЗ копии:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\ochistka_redakcii_k1a.ps1" -NoBackup
param([string]$Root = "D:\kniga1a", [switch]$NoBackup)
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$leaf = (Split-Path $Root -Leaf).ToLower()
$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
if (-not (Test-Path -LiteralPath $Root)) { Write-Host ("СТОП: папки " + $Root + " нет") -ForegroundColor Red; exit 1 }

Write-Host ("=== 1. Останавливаю озвучку, студию и проверки книги " + $Root) -ForegroundColor Cyan
$n = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like ("*" + $leaf + "*") -and
                   ($_.CommandLine -like "*nox_fast*" -or $_.CommandLine -like "*nox_audiobook.py*" -or $_.CommandLine -like "*nox_studio*" -or
                    $_.CommandLine -like "*verify_k1a*" -or $_.CommandLine -like "*voicecheck_k1a*" -or $_.CommandLine -like "*audit_k1a*" -or
                    $_.CommandLine -like "*razmetka_k1a*" -or $_.CommandLine -like "*zvuk_k1a*" -or $_.CommandLine -like "*nox_part*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
Start-Sleep 3
Write-Host ("  остановлено процессов: " + $n)

if (-not $NoBackup) {
    $arch = Join-Path (Split-Path $Root -Parent) ($leaf + "_arhiv_" + $stamp)
    Write-Host ("=== 2. Копия важного (без звука, частей и готовых глав) -> " + $arch) -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $arch | Out-Null
    $skipWork = @((Join-Path $Root "work\wav"))
    $skipStudio = @("takes", "cache", "render", "trash", "sounds", "imports") | ForEach-Object { Join-Path $Root ("studio\" + $_) }
    if (Test-Path -LiteralPath (Join-Path $Root "work")) {
        & robocopy (Join-Path $Root "work") (Join-Path $arch "work") /E /XD $skipWork /XF *.tmp /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    if (Test-Path -LiteralPath (Join-Path $Root "studio")) {
        & robocopy (Join-Path $Root "studio") (Join-Path $arch "studio") /E /XD $skipStudio /XF *.tmp /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    foreach ($f in @("Настройки озвучки.xlsx", "book.docx", "Разметка_книги.txt")) {
        if (Test-Path -LiteralPath (Join-Path $Root $f)) { Copy-Item -LiteralPath (Join-Path $Root $f) -Destination $arch -Force -ErrorAction SilentlyContinue }
    }
    $sz = (Get-ChildItem -LiteralPath $arch -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    Write-Host ("  копия готова: " + [math]::Round($sz / 1MB, 1) + " МБ") -ForegroundColor Green
    if ($sz -lt 1024) { Write-Host "СТОП: копия пустая — ничего не удаляю." -ForegroundColor Red; exit 1 }
} else {
    Write-Host "=== 2. Копия не делается (ключ -NoBackup)" -ForegroundColor Yellow
}

$script:freed = 0
$script:files = 0
function Remove-Quiet([string]$path) {
    $items = @(Get-ChildItem -Path (Split-Path $path) -Filter (Split-Path $path -Leaf) -Force -ErrorAction SilentlyContinue)
    if ($items.Count -eq 0) { return }
    $sz = 0; $cnt = 0
    foreach ($it in $items) {
        if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }      # ссылки на окружения — никогда
        if ($it.PSIsContainer) {
            $inner = @(Get-ChildItem -LiteralPath $it.FullName -Recurse -File -Force -ErrorAction SilentlyContinue)
            $sz += ($inner | Measure-Object Length -Sum).Sum; $cnt += $inner.Count
        } else { $sz += $it.Length; $cnt += 1 }
        Remove-Item -LiteralPath $it.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    $script:freed += $sz; $script:files += $cnt
    Write-Host ("  - " + $path + "  (" + $cnt + " файл., " + [math]::Round($sz / 1MB, 1) + " МБ)")
}

Write-Host "=== 3. Удаляю озвучку старого текста, части и готовые главы" -ForegroundColor Cyan
if (Test-Path -LiteralPath (Join-Path $Root "work")) {
    $keep = @("voices", "voices_old", "roles.json", "settings.json", "accent_dict.csv", "speaker_overrides.tsv", "stress_words.txt",
              "stress_fixes.tsv", "voicecheck_tries.json", "voicecheck.json", "voicecheck.txt")
    Get-ChildItem -LiteralPath (Join-Path $Root "work") -Force -ErrorAction SilentlyContinue |
        Where-Object { $keep -notcontains $_.Name } |
        ForEach-Object { Remove-Quiet (Join-Path (Join-Path $Root "work") $_.Name) }
}
foreach ($m in @("parts", "parts_zvuk", "sample", "gotovo", "__pycache__", "book.mp3", "book.m4b", "Разметка_книги.txt", "part.log", "part.err.log",
                 "fast_w*.log", "log_fast.txt", "verify.log", "verify.err.log", "studio_go.log", "studio_engine.log")) {
    Remove-Quiet (Join-Path $Root $m)
}

Write-Host "=== 4. Студия: чистый лист (записи, закрепления, правки; библиотека звуков остаётся)" -ForegroundColor Cyan
if (Test-Path -LiteralPath (Join-Path $Root "studio")) {
    foreach ($m in @("takes", "cache", "render", "trash", "edits.json", "edits.log", "edits.json.*", "fixed_parts.json", "tech_cache.json", "studio.log", "backup")) {
        Remove-Quiet (Join-Path (Join-Path $Root "studio") $m)
    }
    Write-Host "  НЕ ТРОНУТЫ: studio\sounds (библиотека звуков), studio\imports, проекты"
}

Write-Host ""
Write-Host ("ЗАЧИСТКА ЗАВЕРШЕНА: удалено " + $script:files + " файлов, освобождено " + [math]::Round($script:freed / 1GB, 2) + " ГБ.") -ForegroundColor Green
Write-Host "Яндекс Диск не тронут: прежние собранные части лежат там. Голоса персонажей, роли, настройки и рукопись остались."
if (-not $NoBackup) { Write-Host ("Копия правок студии и данных: " + $arch) }
Write-Host "Дальше по порядку: go_kniga1a.ps1 -PrepOnly  (разбивка новой книги и отметки звука), потом обычный go."
