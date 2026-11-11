# v_github.ps1 — NOX_GIT 2026-11-11b. Публикация КОДА на GitHub: только папка nox-audiobook из архива nox_github_*.zip.
# Рукопись, озвучка, роли, правки студии и вся папка книги в публикацию не попадают — это проверяется перед отправкой.
# Запуск (одной строкой, из любого окна PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File (Get-ChildItem "$HOME\Downloads\v_github*.ps1" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
# Ничего не удаляет в папке книги. Повторный запуск с новым архивом обновляет репозиторий.
# С рабочей машины добавляет в data снимок среды (D:\nox_audiobook\lock: точные версии пакетов, Python, драйвер, модели) —
# по нему tools\ustanovka_s_nulya.ps1 ставит на чистом компьютере те же версии. Имя пользователя и компьютера из снимка убираются.
param(
    [string]$Repo = "https://github.com/justfortune/text_voice.git",
    [string]$Dir = "D:\nox-audiobook",
    [string]$Book = "D:\kniga1a",
    [string]$Src = "D:\nox_audiobook",
    [string]$Downloads = "",
    [switch]$NoPush
)
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { chcp 65001 | Out-Null } catch {}
if (-not $Downloads) { $Downloads = Join-Path $env:USERPROFILE "Downloads" }
$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
function Say([string]$text, [string]$color = "") { if ($color) { Write-Host $text -ForegroundColor $color } else { Write-Host $text } }
$utf8 = New-Object Text.UTF8Encoding $false
function CleanText([string]$text) {                        # пути вида C:\Users\имя и имена пользователя/компьютера — прочь из публикации
    $res = [regex]::Replace($text, '(?i)([\\/])Users([\\/]+)[^\\/\r\n"'']+', '$1Users$2USER')
    foreach ($secret in @($env:USERNAME, $env:COMPUTERNAME)) {
        if ($secret -and $secret.Length -ge 3) { $res = [regex]::Replace($res, [regex]::Escape($secret), "USER", [Text.RegularExpressions.RegexOptions]::IgnoreCase) }
    }
    return $res
}
function Stop-Here([string]$text) { Say ""; Say ("СТОП: " + $text) "Red"; Say "Ничего не отправлено." "Red"; exit 1 }

Say ("Публикация кода на GitHub: " + $Repo) "Cyan"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Here "программа git не найдена. В PyCharm: File - Settings - Version Control - Git - Download and Install, потом запустите эту команду снова." }
$owner = ""
if ($Repo -match 'github\.com[:/]+([^/]+)/') { $owner = $Matches[1] }

# ------------------------------------------------------------------ 1. папка книги: в git из неё не должно попасть ничего
Say ""
Say "1. Папка книги — под защитой" "Cyan"
if (Test-Path -LiteralPath $Book) {
    $guard = Join-Path $Book ".gitignore"
    $guardText = "# Nox: папка книги (рукопись, озвучка, правки студии) не публикуется. Код для публикации лежит отдельно.`r`n*`r`n"
    if (-not (Test-Path -LiteralPath $guard)) {
        [IO.File]::WriteAllText($guard, $guardText, $utf8)
        Say ("  + в папке книги создан .gitignore со звёздочкой: git не возьмёт из неё ни одного файла") "Green"
    } else {
        $cur = ""; try { $cur = [IO.File]::ReadAllText($guard) } catch {}
        if ($cur -match '(?m)^\*\s*$') { Say "  страховка на месте: .gitignore в папке книги закрывает всё её содержимое" }
        else {
            [IO.File]::WriteAllText($guard, ($cur.TrimEnd() + "`r`n" + $guardText), $utf8)
            Say "  + в .gitignore папки книги дописана звёздочка: теперь git не возьмёт из неё ни одного файла" "Green"
        }
    }
    $bgit = Join-Path $Book ".git"
    if (Test-Path -LiteralPath $bgit) {
        $heads = @(Get-ChildItem -LiteralPath (Join-Path $bgit "refs/heads") -Recurse -File -Force -ErrorAction SilentlyContinue)
        $hasCommits = ($heads.Count -gt 0) -or (Test-Path -LiteralPath (Join-Path $bgit "packed-refs"))
        $cfgText = ""; try { $cfgText = [IO.File]::ReadAllText((Join-Path $bgit "config")) } catch {}
        $remoteUrl = ""
        if ($cfgText -match '(?m)^\s*url\s*=\s*(\S+)') { $remoteUrl = $Matches[1] }
        if ($hasCommits) {
            Say "  ! В папке книги есть git-репозиторий, и в нём уже есть сохранённые версии (коммиты). Я его не трогаю." "Yellow"
            if ($remoteUrl) { Say ("  ! Он привязан к " + $remoteUrl + ". Откройте эту страницу: если там видны рукопись, звук или папка work — удалите репозиторий на сайте (Settings - Delete this repository) и напишите мне.") "Red" }
            else { Say "  ! К сайту он не привязан — наружу из него ничего не уходило." "Yellow" }
        } else {
            $aside = Join-Path $Book ("_staroe_" + $stamp)
            try {
                New-Item -ItemType Directory -Force -Path $aside | Out-Null
                Move-Item -LiteralPath $bgit -Destination (Join-Path $aside "_git") -Force -ErrorAction Stop
                Say ("  + пустой git-репозиторий, созданный в папке книги, убран в " + (Split-Path $aside -Leaf) + "\_git (в нём не было ни одной сохранённой версии)") "Green"
                Say "    PyCharm может сказать «Invalid VCS root mapping» — нажмите там Configure, выделите красную строку, минус, OK."
            } catch {
                Say ("  ! пустой git-репозиторий в папке книги убрать не вышло (" + $_.Exception.Message + ") — он остаётся, но .gitignore со звёздочкой не даст взять из папки ни одного файла") "Yellow"
            }
        }
    } else { Say "  git-репозитория в папке книги нет — так и должно быть" }
} else { Say ("  папки " + $Book + " нет — пропускаю") }

# ------------------------------------------------------------------ 2. папка для публикации: только содержимое архива
Say ""
Say "2. Папка с кодом для публикации" "Cyan"
foreach ($sign in @("work", "studio", "gotovo", "book.docx", "nox_audiobook.py")) {
    if (Test-Path -LiteralPath (Join-Path $Dir $sign)) { Stop-Here ("папка " + $Dir + " похожа на папку книги (в ней есть " + $sign + "). Код публикуется только из отдельной папки.") }
}
if ((Test-Path -LiteralPath $Book) -and (Test-Path -LiteralPath $Dir) -and ((Resolve-Path -LiteralPath $Dir).Path -ieq (Resolve-Path -LiteralPath $Book).Path)) { Stop-Here "папка для публикации совпадает с папкой книги" }
$zip = @(Get-ChildItem -LiteralPath $Downloads -Filter "nox_github_*.zip" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending) | Select-Object -First 1
$need = @("README.md", ".gitignore", ".gitattributes", "kit/go_kniga1a.ps1", "kit/nox_studio.py", "kit/nox_studio.html", "tools/sborshchik_k1a.ps1", "base/nox_audiobook.py")
$parts = @("kit", "tools", "base", "data")
if ($zip) {
    $tmpx = Join-Path ([IO.Path]::GetTempPath()) ("nox_gh_" + $stamp)
    try {
        Expand-Archive -LiteralPath $zip.FullName -DestinationPath $tmpx -Force -ErrorAction Stop
        $srcDir = Join-Path $tmpx "nox-audiobook"
        $miss = @($need | Where-Object { -not (Test-Path -LiteralPath (Join-Path $srcDir $_)) })
        if ($miss.Count -gt 0) { Stop-Here ("архив " + $zip.Name + " не тот или скачался не целиком (нет " + ($miss -join ", ") + "). Скачайте nox_github_*.zip из чата заново.") }
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        foreach ($p in $parts) {                           # папка — точная копия архива: прежние файлы комплекта заменяются целиком
            $dst = Join-Path $Dir $p
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force -ErrorAction Stop }
            if (Test-Path -LiteralPath (Join-Path $srcDir $p)) { Copy-Item -LiteralPath (Join-Path $srcDir $p) -Destination $dst -Recurse -Force -ErrorAction Stop }
        }
        foreach ($f in @("README.md", ".gitignore", ".gitattributes")) { Copy-Item -LiteralPath (Join-Path $srcDir $f) -Destination (Join-Path $Dir $f) -Force -ErrorAction Stop }
        Say ("  + " + $Dir + " собрана из архива " + $zip.Name) "Green"
    } catch { Stop-Here ("архив " + $zip.Name + " не распаковался: " + $_.Exception.Message) }
    finally { if (Test-Path -LiteralPath $tmpx) { Remove-Item -LiteralPath $tmpx -Recurse -Force -ErrorAction SilentlyContinue } }
} elseif (Test-Path -LiteralPath (Join-Path $Dir "kit/go_kniga1a.ps1")) {
    Say ("  архива nox_github_*.zip в Загрузках нет — беру папку " + $Dir + " как есть")
} else {
    Stop-Here ("в Загрузках нет архива nox_github_*.zip, а папки " + $Dir + " ещё нет. Скачайте nox_github_2026-11-11g.zip из чата (распаковывать не нужно) и запустите команду снова.")
}
$miss = @($need | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Dir $_)) })
if ($miss.Count -gt 0) { Stop-Here ("в папке " + $Dir + " не хватает: " + ($miss -join ", ")) }
# снимок среды рабочей машины -> data\sreda_*: по нему среду можно поставить с нуля теми же версиями
$lockDir = Join-Path $Src "lock"; $nl = 0
foreach ($pair in @(@("envfast.lock.txt", "sreda_envfast.txt"), @("env.lock.txt", "sreda_env.txt"), @("manifest.json", "sreda_manifest.json"))) {
    $from = Join-Path $lockDir $pair[0]
    if (Test-Path -LiteralPath $from) {
        try { [IO.File]::WriteAllText((Join-Path (Join-Path $Dir "data") $pair[1]), (CleanText ([IO.File]::ReadAllText($from, [Text.Encoding]::UTF8))), $utf8); $nl++ }
        catch { Say ("  ! снимок " + $pair[0] + " не скопирован: " + $_.Exception.Message) "Yellow" }
    }
}
if ($nl -gt 0) { Say ("  + снимок среды этой машины добавлен в data (файлов: " + $nl + "): точные версии пакетов, Python, драйвер, модели") "Green" }
else { Say ("  снимка среды в " + $lockDir + " нет — в репозитории будут только ключевые версии пакетов (data\versii_*.txt)") }
$kitVer = ""
try { if ((Get-Content -LiteralPath (Join-Path $Dir "kit/go_kniga1a.ps1") -TotalCount 80 -Encoding UTF8 | Out-String) -match 'NOX_KIT (\d{4}-\d\d-\d\d[a-z]?)') { $kitVer = $Matches[1] } } catch {}

# ------------------------------------------------------------------ 3. git: взять файлы и проверить, что среди них нет лишнего
Say ""
Say "3. Что именно уйдёт на GitHub" "Cyan"
$dirGit = ($Dir -replace '\\', '/')
$safe = @(& git config --global --get-all safe.directory)
if (-not ($safe | Where-Object { $_ -ieq $dirGit })) { & git config --global --add safe.directory $dirGit }
if (-not (Test-Path -LiteralPath (Join-Path $Dir ".git"))) { & git -C $Dir init -q; if ($LASTEXITCODE -ne 0) { Stop-Here "git init не выполнился — пришлите этот вывод в чат" } }
$who = $(if ($owner) { $owner } else { "nox" })
& git -C $Dir config user.name $who                        # подпись версий — логин GitHub и почта-заглушка GitHub:
& git -C $Dir config user.email ($who + "@users.noreply.github.com")   # настоящая почта в публикацию не попадает
& git -C $Dir config core.quotepath off
& git -C $Dir add -A
if ($LASTEXITCODE -ne 0) { Stop-Here "git add не выполнился — пришлите этот вывод в чат" }
$tracked = @(& git -C $Dir ls-files)
$okPath = '^(kit/|tools/|base/|data/)[^/]+$|^(README\.md|\.gitignore|\.gitattributes)$'
$badExt = '\.(docx?|pdf|epub|fb2|wav|mp3|m4a|m4b|flac|ogg|opus|noxproj|xlsx?|zip|7z|rar|safetensors|bin|pt|log)$'
$badName = '(speaker_overrides|stress_fixes|segments|roles|edits|kniga\d|book)[^/]*\.(tsv|csv|json|docx|txt)$'
$bad = @($tracked | Where-Object { ($_ -notmatch $okPath) -or ($_ -match $badExt) -or ($_ -match $badName) })
$bytes = [int64]0
foreach ($t in $tracked) { $fi = Get-Item -LiteralPath (Join-Path $Dir $t) -Force -ErrorAction SilentlyContinue; if ($fi) { $bytes += $fi.Length } }
if ($bad.Count -gt 0) {
    & git -C $Dir reset -q
    Stop-Here ("среди файлов оказалось то, чему в публикации не место: " + (($bad | Select-Object -First 8) -join ", ") + ". Уберите это из " + $Dir + " и запустите снова.")
}
if ($tracked.Count -lt 20 -or $tracked.Count -gt 80 -or $bytes -gt 8MB) {
    & git -C $Dir reset -q
    Stop-Here ("набор файлов не похож на комплект: файлов " + $tracked.Count + ", объём " + [math]::Round($bytes / 1MB, 1) + " МБ (ждал 20-80 файлов и не больше 8 МБ). Пришлите этот вывод в чат.")
}
Say ("  файлов: " + $tracked.Count + ", объём " + [math]::Round($bytes / 1MB, 2) + " МБ — только код: kit (" + @($tracked | Where-Object { $_ -like "kit/*" }).Count + "), tools, base, data, README")
Say "  рукописи, звука, ролей, правок студии, таблиц и архивов среди них нет — проверено по списку" "Green"

$msg = "Nox Audiobook: kit " + $(if ($kitVer) { $kitVer } else { $stamp }) + $(if ($zip) { " (" + $zip.BaseName + ")" } else { "" })
& git -C $Dir rev-parse -q --verify HEAD | Out-Null
$hasHead = ($LASTEXITCODE -eq 0)
$rem = @(& git -C $Dir remote)
if ($rem -contains "origin") { & git -C $Dir remote set-url origin $Repo } else { & git -C $Dir remote add origin $Repo }
$remoteHead = @()
if (-not $NoPush) {
    Say ""
    Say "4. Отправка (при первом разе откроется окно входа в GitHub — войдите в свой аккаунт)" "Cyan"
    $remoteHead = @(& git -C $Dir ls-remote --heads origin main)
    if ($LASTEXITCODE -ne 0) { Stop-Here ("GitHub не ответил или вход не выполнен. Проверьте адрес " + $Repo + " и интернет, потом запустите команду снова.") }
    if ($hasHead -and $remoteHead.Count -eq 0) {
        # в папке уже есть местные версии (например, их сделал PyCharm), а на GitHub пусто. Если они подписаны не почтой-заглушкой,
        # настоящая почта стала бы видна всем: заменяю местную историю одной версией с подписью-заглушкой (файлы те же)
        $mails = @(& git -C $Dir log --format=%ae%n%ce | Sort-Object -Unique)
        $foreign = @($mails | Where-Object { $_ -and ($_ -notlike "*@users.noreply.github.com") })
        if ($foreign.Count -gt 0) {
            $tree = "$(& git -C $Dir write-tree)".Trim()
            $fresh = "$(& git -C $Dir commit-tree $tree -m $msg)".Trim()
            if ($LASTEXITCODE -ne 0 -or -not $fresh) { Stop-Here "не вышло пересобрать местную историю — пришлите этот вывод в чат" }
            & git -C $Dir reset -q --soft $fresh
            Say "  + прежние местные версии были подписаны другой почтой — заменены одной версией с почтой-заглушкой GitHub (файлы те же)" "Green"
        }
    }
}
& git -C $Dir diff --cached --quiet
$changed = ($LASTEXITCODE -ne 0)
if ($changed -or -not $hasHead) {
    & git -C $Dir commit -q -m $msg
    if ($LASTEXITCODE -ne 0) { Stop-Here "git commit не выполнился — пришлите этот вывод в чат" }
    Say ("  + версия сохранена: комплект " + $kitVer) "Green"
} elseif ($NoPush) { Say "  с прошлого раза файлы не менялись — новой версии нет" }
& git -C $Dir branch -M main
if ($NoPush) { Say ""; Say "Ключ -NoPush: всё готово в папке, отправка не выполнялась." "Yellow"; exit 0 }
$mine = "$(& git -C $Dir rev-parse HEAD)".Trim()
if ($remoteHead.Count -gt 0) {
    $theirs = ("$($remoteHead[0])" -split '\s+')[0]
    if ($theirs -eq $mine) { Say ("Уже опубликовано: на GitHub та же версия. Смотреть: " + ($Repo -replace '\.git$', '')) "Green"; exit 0 }
    & git -C $Dir merge-base --is-ancestor $theirs HEAD 2>$null
    if ($LASTEXITCODE -ne 0) {
        # там версия, которой здесь нет. Если это наша же прежняя публикация кода (папку пересоздали) — продолжаем её историю;
        # если там что-то другое — ничего не трогаем и не затираем
        & git -C $Dir fetch -q origin main
        $theirFiles = @()
        if ($LASTEXITCODE -eq 0) { $theirFiles = @(& git -C $Dir ls-tree -r --name-only FETCH_HEAD) }
        $alien = @($theirFiles | Where-Object { ($_ -notmatch $okPath) -or ($_ -match $badExt) -or ($_ -match $badName) })
        if ($theirFiles.Count -gt 0 -and $alien.Count -eq 0) {
            & git -C $Dir reset -q --soft FETCH_HEAD
            & git -C $Dir diff --cached --quiet
            if ($LASTEXITCODE -ne 0) {
                & git -C $Dir commit -q -m $msg
                if ($LASTEXITCODE -ne 0) { Stop-Here "git commit не выполнился — пришлите этот вывод в чат" }
            }
            $mine = "$(& git -C $Dir rev-parse HEAD)".Trim()
            Say "  на GitHub лежит прежняя публикация кода — продолжаю её историю"
            if ($theirs -eq $mine) { Say ("Уже опубликовано: на GitHub те же файлы. Смотреть: " + ($Repo -replace '\.git$', '')) "Green"; exit 0 }
        } else {
        Stop-Here ("в репозитории на GitHub уже лежит что-то, отправленное не этой командой. Откройте " + ($Repo -replace '\.git$', '') + " и посмотрите, что там. Если это файлы книги или звук — удалите репозиторий на сайте (Settings - внизу Delete this repository), создайте пустой с тем же именем и запустите команду снова.")
        }
    }
}
& git -C $Dir push -u origin main
if ($LASTEXITCODE -ne 0) { Stop-Here "отправка не прошла — текст причины выше. Чаще всего это отменённый вход в GitHub: запустите команду ещё раз и войдите." }
$after = @(& git -C $Dir ls-remote --heads origin main)
$theirs = $(if ($after.Count -gt 0) { ("$($after[0])" -split '\s+')[0] } else { "" })
if ($theirs -ne $mine) { Stop-Here "GitHub принял отправку, но версия на сайте не совпала с местной — пришлите этот вывод в чат" }
Say ""
Say ("ГОТОВО: опубликовано файлов " + $tracked.Count + " (комплект " + $kitVer + "). Смотреть: " + ($Repo -replace '\.git$', '')) "Green"
Say "Рукопись, озвучка и правки в папке книги не затронуты и в публикацию не входят. Чтобы обновить репозиторий позже — скачайте новый nox_github_*.zip и запустите эту же команду."
exit 0
