# sborshchik_k1a.ps1 — СБОРЩИК-ПРОВЕРКА: что с книгой сейчас, что сломано, что починится само и что делать по порядку.
# NOX_SBOR 2026-11-11i
#
# ТОЛЬКО ЧТЕНИЕ. Без ключа -Fix скрипт ничего не меняет, не удаляет и не останавливает ни в D:\kniga1a, ни в
# D:\nox_audiobook, ни в Загрузках. Он смотрит:
#   1) компьютер: место на дисках, видеокарту, что сейчас запущено по этой книге;
#   2) общую среду D:\nox_audiobook: python, пакеты, модели, исходник конвейера (сверяется с эталоном);
#   3) папку книги: ссылки на среду, рукопись, таблицу настроек, файл конвейера, чужие и старые скрипты;
#   4) рабочие данные work: разбивку книги, роли, образцы голосов, сколько и какой озвучки лежит;
#   5) Загрузки: все ли файлы комплекта свежие, есть ли сценарий ролей, словарь, ударения по месту;
#   6) студию и Яндекс Диск.
# В конце — ИТОГ и «ЧТО ДЕЛАТЬ ПО ПОРЯДКУ» с готовыми командами. Тот же текст ложится в файл отчёта, а вместе с мелкими
# служебными файлами (без звука и моделей) — в zip в Загрузках: его можно приложить в чат.
#
# Пометки:  [ОК] — в порядке;  [САМО] — сломано или устарело, но go / зачистка исправят сами;
#           [НАДО] — нужно ваше действие (или ключ -Fix);  [СТОП] — без этого запуск не пойдёт.
#
# Ключ -Fix — безопасные исправления найденного: разложить в Загрузки свежий комплект из архива kit_nox_<версия>.zip
#   (прежние версии файлов уходят в Загрузки\_nox_old), вернуть в Загрузки недостающие файлы данных из папки книги, восстановить
#   исходник конвейера из целой копии, убрать чужие и старые скрипты в D:\kniga1a\_staroe_<дата>, положить на рабочий стол
#   ярлыки «Nox go» (озвучить следующую часть) и «Nox proverka» (этот сборщик). НИЧЕГО НЕ УДАЛЯЕТСЯ: файлы только копируются
#   или переносятся, всё можно вернуть руками.
#
# Откуда запускать: обычное окно PowerShell (Win+R, powershell, Enter). PyCharm, activate, conda activate НЕ НУЖНЫ: все скрипты
#   сами вызывают свой python по полному пути. Сборщик можно запустить и из терминала PyCharm, а вот go — только в отдельном
#   окне: закроете PyCharm — озвучка оборвётся.
#
# Запуск (PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "& (Get-ChildItem $HOME\Downloads\sborshchik_k1a*.ps1 | Sort-Object LastWriteTime | Select-Object -Last 1).FullName"
param(
    [string]$Root = "D:\kniga1a",
    [string]$Src = "D:\nox_audiobook",
    [string]$Downloads = "",
    [string]$Yandex = "",
    [switch]$Fix,
    [switch]$NoZip
)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$IsWin = ($env:OS -eq "Windows_NT")
if ($IsWin) { chcp 65001 | Out-Null }
if (-not $Downloads) { $Downloads = Join-Path $env:USERPROFILE "Downloads" }
if (-not $Yandex) { $Yandex = Join-Path $env:USERPROFILE "YandexDisk" }

$KIT = "2026-11-11i"
$KITFILES = @("go_kniga1a.ps1", "prep_kniga1a.ps1", "run_fast.ps1", "cleanup_k1a.ps1", "nox_fast.py", "verify_k1a.py", "nox_part.py",
              "nox_stress.py", "nox_voices.py", "voicecheck_k1a.py", "nastroyki_ozvuchki.py", "razmetka_k1a.py", "zvuk_k1a.py",
              "voices_kniga1a.py", "nox_studio.py", "nox_studio.html", "nox_studio.ps1", "nox_cues.py", "nox_import.py")
# эталоны: исходник конвейера (без различия концов строк), файлы данных книги 1 и рукопись из комплекта
$BASE_SHA = "4844E7C3AB344B702EA403DA08E4F1E346030CDA"
$DOCX_SHA = "F1504BCA980F13A0FB298DC4A42B329D557A5C11"
$DATA = @(
    @{ name = "speaker_overrides_kniga1a.tsv"; sha = "A5526419A8A0DEF8D20C4E1DEE9875B087DBB4E1"; must = $true;  work = "speaker_overrides.tsv"; what = "сценарий ролей" },
    @{ name = "accent_dict.csv";               sha = "AF4CFB7240274B8F0D5A7EEC124203500AC38938"; must = $false; work = "accent_dict.csv";       what = "словарь произношения" },
    @{ name = "stress_fixes_kniga1a.tsv";      sha = "73C424FA8913C378B9765239CB9B071DAD0D484C"; must = $false; work = "stress_fixes.tsv";      what = "ударения по месту" })
# что оставил другой помощник (не из комплекта) и старые копии пусковых скриптов в папке книги
$FOREIGN = @("clean_start*.ps1", "force_rebuild*.ps1", "run_safe*.ps1", "status_nox*.ps1", "temp_*.py", "*surgeon*.py", "*_fix*.py",
             "nox_system_analysis*", "fix_*.ps1", "fix_*.py", "restart_*.ps1", "safe_*.ps1")
$LAUNCHERS = @("go_kniga1a*.ps1", "prep_kniga1a*.ps1", "run_fast*.ps1", "cleanup_k1a*.ps1", "nox_studio*.ps1", "sborshchik_k1a*.ps1")
$CODEBAK = @("verify_k1a.py.bak_*", "zvuk_k1a.py.bak_*", "nox_fast.py.bak_*", "run_fast.ps1.bak_*", "nox_audiobook.py.bak_2*")

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$leaf = (Split-Path $Root -Leaf).ToLower()
$script:rep = New-Object System.Collections.Generic.List[string]
$script:cnt = @{ ok = 0; self = 0; need = 0; stop = 0 }
$script:fixes = New-Object System.Collections.Generic.List[object]
$script:getKit = New-Object System.Collections.Generic.List[string]
$script:clean = New-Object System.Collections.Generic.List[string]

function Say([string]$text, [string]$color = "") {
    if ($color) { Write-Host $text -ForegroundColor $color } else { Write-Host $text }
    $script:rep.Add($text)
}
function Head([string]$t) { Say ""; Say ("=== " + $t) "Cyan" }
function Ok([string]$t)    { $script:cnt.ok++;   Say ("  [ОК]    " + $t) "Green" }
function Self([string]$t)  { $script:cnt.self++; Say ("  [САМО]  " + $t) "DarkCyan" }
function Need([string]$t)  { $script:cnt.need++; Say ("  [НАДО]  " + $t) "Yellow" }
function Block([string]$t) { $script:cnt.stop++; Say ("  [СТОП]  " + $t) "Red" }
function Info([string]$t)  { Say ("  [инфо]  " + $t) "Gray" }
function Det([string]$t)   { $script:rep.Add("            " + $t) }
function Oops([string]$where, $err) { Say ("  [!]     проверка «" + $where + "» не выполнилась: " + $err.Exception.Message) "Magenta" }

function SizeText($n) {
    $n = [double]$n
    if ($n -ge 1GB) { return ("{0:N1} ГБ" -f ($n / 1GB)) }
    if ($n -ge 1MB) { return ("{0:N1} МБ" -f ($n / 1MB)) }
    return ("{0:N0} КБ" -f [math]::Ceiling($n / 1KB))
}
function DateText($d) { if ($d) { return $d.ToString("dd.MM.yyyy HH:mm") } else { return "—" } }
function Sha1File([string]$p) { try { return (Get-FileHash -LiteralPath $p -Algorithm SHA1 -ErrorAction Stop).Hash } catch { return "" } }
function Sha1Norm([string]$p) {
    # отпечаток текста без различия концов строк (CRLF / LF) и без метки BOM
    try {
        $b = [IO.File]::ReadAllBytes($p)
        $ms = New-Object IO.MemoryStream
        $i0 = 0
        if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) { $i0 = 3 }
        for ($i = $i0; $i -lt $b.Length; $i++) { if ($b[$i] -ne 13) { $ms.WriteByte($b[$i]) } }
        $sha = [Security.Cryptography.SHA1]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash($ms.ToArray()))).Replace("-", "")
    } catch { return "" }
}
function HasBom([string]$p) {
    try {
        $fs = [IO.File]::OpenRead($p); $b = New-Object byte[] 3; $k = $fs.Read($b, 0, 3); $fs.Close()
        return ($k -eq 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
    } catch { return $false }
}
function ReadJsonCS([string]$p) {
    # JSON, где есть ключи, различающиеся только регистром (роли «голос» и «Голос» — разные персонажи): ConvertFrom-Json
    # из Windows PowerShell 5.1 на таком файле падает, хотя файл верный и конвейер (Python) его читает. Разбор с учётом регистра.
    $txt = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
    if ($PSVersionTable.PSVersion.Major -ge 6) { return ($txt | ConvertFrom-Json -AsHashtable) }
    Add-Type -AssemblyName System.Web.Extensions
    $ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $ser.MaxJsonLength = [int]::MaxValue
    return $ser.DeserializeObject($txt)
}
function FindKitArchive {
    # свежий комплект одним файлом: Загрузки\kit_nox_<версия>.zip (или папка kit_nox_…, если архив распаковали руками)
    $c = @(Get-ChildItem -LiteralPath $Downloads -Filter "kit_nox_*" -ErrorAction SilentlyContinue |
           Where-Object { $_.PSIsContainer -or $_.Extension -ieq ".zip" } | Sort-Object LastWriteTime -Descending)
    if ($c.Count -eq 0) { return $null }
    $mine = @($c | Where-Object { $_.Name -like ("*" + $KIT + "*") })
    if ($mine.Count -gt 0) { $c = $mine }
    return @{ item = $c[0]; kind = $(if ($c[0].PSIsContainer) { "dir" } else { "zip" }) }
}
function Install-KitArchive {
    # 19 файлов комплекта из архива -> в Загрузки под правильными именами; прежние версии этих файлов (и копии «(1)») —
    # в Загрузки\_nox_old. Ничего не удаляется. Архив другой версии или неполный не трогает ничего.
    $ka = FindKitArchive
    if (-not $ka) { return }
    Head ("0. Свежий комплект из " + $(if ($ka.kind -eq "zip") { "архива " } else { "папки " }) + $ka.item.Name)
    $tmpx = $null
    try {
        $from = $ka.item.FullName
        if ($ka.kind -eq "zip") {
            $tmpx = Join-Path ([IO.Path]::GetTempPath()) ("nox_kit_" + $stamp)
            if (Test-Path -LiteralPath $tmpx) { Remove-Item -LiteralPath $tmpx -Recurse -Force }
            Expand-Archive -LiteralPath $from -DestinationPath $tmpx -Force -ErrorAction Stop
            $from = $tmpx
        }
        $have = @{}
        foreach ($x in @(Get-ChildItem -LiteralPath $from -Recurse -File -ErrorAction SilentlyContinue)) { if (-not $have.ContainsKey($x.Name)) { $have[$x.Name] = $x } }
        $miss = @($KITFILES | Where-Object { -not $have.ContainsKey($_) })
        $wrong = @($KITFILES | Where-Object { $have.ContainsKey($_) -and ((KitVer $have[$_].FullName) -ne $KIT) })
        if ($miss.Count -gt 0 -or $wrong.Count -gt 0) {
            $why = ""
            if ($miss.Count -gt 0) { $why = "в нём нет " + $(if ($miss.Count -gt 4) { "" + $miss.Count + " файлов из 19" } else { $miss -join ", " }) }
            else { $vv = KitVer $have[$wrong[0]].FullName; $why = "в нём комплект " + $(if ($vv) { $vv } else { "без метки версии" }) + " (" + $wrong.Count + " файлов из 19)" }
            Say ("  ! этому сборщику нужен комплект " + $KIT + ", а " + $why + ". Ничего не тронуто — скачайте архив и сборщик из одного и того же сообщения Claude") "Red"
            return
        }
        $oldDir = Join-Path $Downloads "_nox_old"
        $put = 0; $same = 0; $moved = 0
        foreach ($f in $KITFILES) {
            $shaNew = Sha1File $have[$f].FullName
            $stem = [IO.Path]::GetFileNameWithoutExtension($f); $ext = [IO.Path]::GetExtension($f)
            $fam = @(Get-ChildItem -LiteralPath $Downloads -Filter ($stem + "*" + $ext) -File -ErrorAction SilentlyContinue |
                     Where-Object { ($_.Name -replace ' \(\d+\)(?=\.[^.]+$)', '') -eq $f })
            $target = Join-Path $Downloads $f
            $inPlace = $false
            foreach ($o in $fam) {
                if ($o.Name -eq $f -and (Sha1File $o.FullName) -eq $shaNew) { $inPlace = $true; continue }
                New-Item -ItemType Directory -Force -Path $oldDir | Out-Null
                $dst = Join-Path $oldDir $o.Name
                if (Test-Path -LiteralPath $dst) { $dst = Join-Path $oldDir ($o.BaseName + "_" + $o.LastWriteTime.ToString("MMdd_HHmmss") + $o.Extension) }
                if (Test-Path -LiteralPath $dst) { $dst = Join-Path $oldDir ($o.BaseName + "_" + $stamp + $o.Extension) }
                Move-Item -LiteralPath $o.FullName -Destination $dst -ErrorAction Stop
                $moved++
            }
            if ($inPlace) { $same++ } else { Copy-Item -LiteralPath $have[$f].FullName -Destination $target -ErrorAction Stop; $put++ }
            (Get-Item -LiteralPath $target).LastWriteTime = Get-Date
        }
        Say ("  + комплект " + $KIT + " разложен в Загрузки: файлов " + $KITFILES.Count + " (новых " + $put + ", уже были на месте " + $same + ")" + $(if ($moved -gt 0) { "; прежние версии и копии «(1)» — " + $moved + " шт. — убраны в " + $oldDir } else { "" })) "Green"
    } catch { Say ("  ! комплект из архива не разложен (" + $_.Exception.Message + "). Скачайте архив заново; не поможет — распакуйте его сами и положите все файлы из него прямо в Загрузки") "Red" }
    finally { if ($tmpx -and (Test-Path -LiteralPath $tmpx)) { Remove-Item -LiteralPath $tmpx -Recurse -Force -ErrorAction SilentlyContinue } }
}
function RolesProbe([string]$file) {
    # читает roles.json так же, как конвейер (python общей среды): @{ n; bom; twins; err; how }
    $r = @{ n = 0; bom = $false; twins = @(); err = ""; how = "" }
    foreach ($py in @((Join-Path (Join-Path (Join-Path $Src "envfast") "Scripts") "python.exe"), (Join-Path (Join-Path $Src "env") "python.exe"))) {
        if (-not (Test-Path -LiteralPath $py) -or -not (Test-Path -LiteralPath $script:rolesProbe)) { continue }
        try {
            $out = @(& $py $script:rolesProbe $file 2>$null)
            if ($out.Count -lt 1) { continue }
            $w = @("$($out[0])".Trim().Split(" "))
            if ($w[0] -eq "OK" -and $w.Count -ge 3) {
                $r.n = [int]$w[1]; $r.bom = ($w[2] -eq "1"); $r.how = "python"
                for ($i = 1; $i -lt $out.Count; $i++) { $ln = "$($out[$i])".Trim(); if ($ln) { try { $r.twins += [regex]::Unescape($ln) } catch { $r.twins += $ln } } }
                return $r
            }
            if ($w[0] -eq "ERR") { $r.err = "$($out[0])".Trim().Substring(3).Trim(); $r.how = "python"; return $r }
        } catch {}
    }
    try {      # python недоступен — разбор средствами Windows
        $j = ReadJsonCS $file
        $rd = $null; if ($j) { $rd = $j["roles"] }
        if ($rd) {
            $r.n = @($rd.Keys).Count
            $r.twins = @(@($rd.Keys) | Group-Object { "$_".ToLowerInvariant() } | Where-Object { $_.Count -gt 1 } | ForEach-Object { ($_.Group | Sort-Object) -join " / " })
        }
        $r.bom = HasBom $file; $r.how = "net"
    } catch { $r.err = $_.Exception.Message; $r.how = "net" }
    return $r
}
function KitVer([string]$p) {
    try {
        $m = Select-String -LiteralPath $p -Pattern 'NOX_(KIT|STUDIO) (\d{4}-\d{2}-\d{2}[a-z]?)' -Encoding UTF8 -ErrorAction Stop | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[2].Value }
    } catch {}
    return ""
}
function DirStat([string]$dir, [string]$pattern = "*") {
    $r = @{ n = 0; bytes = [int64]0; newest = $null; oldest = $null }
    if (-not (Test-Path -LiteralPath $dir)) { return $r }
    try {
        foreach ($f in [IO.Directory]::EnumerateFiles($dir, $pattern, [IO.SearchOption]::AllDirectories)) {
            $fi = New-Object IO.FileInfo $f
            $r.n++; $r.bytes += $fi.Length
            if (-not $r.newest -or $fi.LastWriteTime -gt $r.newest) { $r.newest = $fi.LastWriteTime }
            if (-not $r.oldest -or $fi.LastWriteTime -lt $r.oldest) { $r.oldest = $fi.LastWriteTime }
        }
    } catch {}
    return $r
}
function Newest([string]$dir, [string]$name) {
    # самый свежий файл семейства: «имя.ext», «имя (1).ext», «имя (2).ext»
    $base = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name)
    $all = @(Get-ChildItem -LiteralPath $dir -Filter ($base + "*" + $ext) -File -ErrorAction SilentlyContinue |
             Where-Object { ($_.Name -replace ' \(\d+\)(?=\.[^.]+$)', '') -eq $name })
    if ($all.Count -eq 0) { return $null }
    return ($all | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}
$script:probeFile = Join-Path ([IO.Path]::GetTempPath()) ("nox_probe_" + $stamp + ".py")
$probeCode = @'
import sys, importlib.util as u
try:
    import importlib.metadata as md
except Exception:
    md = None
print(sys.version.split()[0])
print(",".join(m for m in sys.argv[1].split(",") if u.find_spec(m) is None))
out = []
for d in sys.argv[2].split(","):
    try:
        out.append(d + "=" + md.version(d))
    except Exception:
        pass
print(";".join(out))
'@
try { [IO.File]::WriteAllText($script:probeFile, $probeCode, (New-Object Text.UTF8Encoding $false)) } catch {}
$script:rolesProbe = Join-Path ([IO.Path]::GetTempPath()) ("nox_roles_" + $stamp + ".py")
$rolesCode = @'
import json, sys
def asc(s):
    return s.encode("ascii", "backslashreplace").decode("ascii")
try:
    b = open(sys.argv[1], "rb").read()
    d = json.loads(b.decode("utf-8-sig"))
    names = list(d["roles"])
    low = {}
    for n in names:
        low.setdefault(n.lower(), []).append(n)
    print("OK", len(names), 1 if b[:3] == b"\xef\xbb\xbf" else 0)
    for v in low.values():
        if len(v) > 1:
            print(asc(" / ".join(v)))
except Exception as e:
    print("ERR", asc(type(e).__name__ + ": " + str(e))[:300])
'@
try { [IO.File]::WriteAllText($script:rolesProbe, $rolesCode, (New-Object Text.UTF8Encoding $false)) } catch {}
$DISTS = @("torch", "torchaudio", "numpy", "transformers", "soundfile", "python-docx", "qwen-tts", "qwen-tts-hf", "faster-qwen3-tts",
           "faster-whisper", "ctranslate2", "ruaccent", "silero-stress", "onnxruntime", "openpyxl", "scipy", "librosa")
function PyProbe([string]$py, [string[]]$mods) {
    $r = @{ ok = $false; ver = ""; missing = @(); pkgs = "" }
    if (-not (Test-Path -LiteralPath $py)) { return $r }
    try {
        $out = @(& $py $script:probeFile ($mods -join ",") ($DISTS -join ",") 2>$null)
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 1) {
            $r.ok = $true; $r.ver = "$($out[0])".Trim()
            if ($out.Count -gt 1 -and "$($out[1])".Trim()) { $r.missing = @("$($out[1])".Trim().Split(",")) }
            if ($out.Count -gt 2) { $r.pkgs = "$($out[2])".Trim() }
        }
    } catch {}
    return $r
}
function PsArgs([string]$pattern, [string]$tail = "", [bool]$stay = $false) {
    $s = "-NoProfile -ExecutionPolicy Bypass "
    if ($stay) { $s += "-NoExit " }
    return ($s + '-Command "& (Get-ChildItem $HOME\Downloads\' + $pattern + ' | Sort-Object LastWriteTime | Select-Object -Last 1).FullName' + $tail + '"')
}
function RunLine([string]$pattern, [string]$tail = "") { return ("powershell " + (PsArgs $pattern $tail $false)) }
$rootTail = ""
if ($Root.TrimEnd('\') -ine "D:\kniga1a") { $rootTail = " -Root '" + $Root + "'" }

Say ("СБОРЩИК-ПРОВЕРКА книги " + $Root + "  —  " + (Get-Date -Format "dd.MM.yyyy HH:mm") + "  (комплект " + $KIT + ")") "White"
if ($Fix) { Say "Режим: ИСПРАВЛЕНИЕ (-Fix) — безопасные правки; ничего не удаляется, только копируется и переносится." "Yellow" }
if ($Fix -and (Test-Path -LiteralPath $Downloads)) { Install-KitArchive }
else { Say "Режим: только чтение — ничего не меняется." }

# ---------------------------------------------------------------------------------------------- 1. компьютер
Head "1. Компьютер"
try {
    Info ("PowerShell " + $PSVersionTable.PSVersion.ToString() + ", пользователь " + $env:USERNAME)
    foreach ($drv in @(@($Root.Substring(0, 1), $Src.Substring(0, 1), "C") | Select-Object -Unique)) {
        try {
            $d = Get-PSDrive -Name $drv -ErrorAction Stop
            $t = "диск " + $drv + ": свободно " + (SizeText $d.Free)
            if ($drv -eq $Root.Substring(0, 1) -and $d.Free -lt 20GB) { Need ($t + " — мало: озвучке книги и моделям нужно 20–30 ГБ") }
            else { Ok $t }
        } catch {}
    }
    $inIde = ("$env:TERMINAL_EMULATOR" -like "*JetBrains*") -or [bool]$env:PYCHARM_HOSTED -or ("$env:TERM_PROGRAM" -like "*vscode*")
    if ($inIde) { Need "сборщик запущен из терминала PyCharm. Проверке это не мешает, но go и зачистку запускайте в отдельном окне: Win+R, powershell, Enter (или ярлыком «Nox go» — его создаёт ключ -Fix). Закроете PyCharm или вкладку терминала — озвучка оборвётся" }
    else { Ok "запущено в обычном окне PowerShell" }
    if ($env:VIRTUAL_ENV) {
        if ("$($env:VIRTUAL_ENV)".ToLower().StartsWith("$Src".ToLower()) -or "$($env:VIRTUAL_ENV)".ToLower().StartsWith("$Root".ToLower())) { Info ("в этом окне активировано окружение конвейера (" + $env:VIRTUAL_ENV + ") — запуску это не мешает и для него не нужно") }
        else { Info ("в этом окне активировано окружение другого проекта (" + $env:VIRTUAL_ENV + ") — конвейеру оно не нужно и не мешает") }
    }
    if ($env:CONDA_DEFAULT_ENV) { Info ("«(" + $env:CONDA_DEFAULT_ENV + ")» в начале строки — это conda из вашего профиля PowerShell; конвейеру она не нужна и не мешает") }
    Info "activate и conda activate для озвучки НЕ НУЖНЫ: скрипты сами вызывают свой python по полному пути"
    foreach ($v in @("PYTHONHOME", "PYTHONPATH")) {
        $val = [Environment]::GetEnvironmentVariable($v)
        if ($val) { Need ("в этом окне задана переменная " + $v + " — она может подменить пакеты python конвейера. Запускайте go в обычном окне PowerShell (Win+R, powershell), а не в терминале PyCharm") }
    }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        $g = @(& nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>$null)
        if ($g.Count -gt 0 -and "$($g[0])".Trim()) { Ok ("видеокарта: " + "$($g[0])".Trim() + "  (занято, всего)") } else { Need "nvidia-smi не отвечает — проверьте драйвер видеокарты" }
    } elseif ($IsWin) { Need "nvidia-smi не найден — без видеокарты NVIDIA озвучка не пойдёт" }
} catch { Oops "компьютер" $_ }

$procs = @()
try {
    if ($IsWin) {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue |
                   Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -notlike "*sborshchik*" -and
                                  ($_.CommandLine -like ("*" + $leaf + "*") -or $_.CommandLine -like "*nox_fast*" -or $_.CommandLine -like "*nox_studio*") })
    }
    if ($procs.Count -eq 0) { Ok "по этой книге ничего не запущено" }
    else {
        $st = @($procs | Where-Object { $_.CommandLine -like "*nox_studio*" })
        $work = @($procs | Where-Object { $_.CommandLine -notlike "*nox_studio*" })
        if ($work.Count -gt 0) { Self ("запущено процессов озвучки, проверки или старых окон этой книги: " + $work.Count + " — go и зачистка остановят их сами; пустые окна PowerShell закройте крестиком") }
        if ($st.Count -gt 0) { Info "«Нокс Студия» сейчас открыта — на обновление не влияет: новая версия сама сменит прежнюю" }
        foreach ($p in $procs) { $c = "$($p.CommandLine)"; if ($c.Length -gt 170) { $c = $c.Substring(0, 170) + "…" }; Det ("PID " + $p.ProcessId + "  " + $p.Name + "  " + $c) }
    }
} catch { Oops "процессы" $_ }

# ---------------------------------------------------------------------------------------------- 2. общая среда
Head ("2. Общая среда " + $Src + " (python, модели, исходник конвейера)")
$srcOk = $true
$srcPy = Join-Path $Src "nox_audiobook.py"
$pristine = $null                                    # где лежит целая копия исходника (если сам он испорчен)
try {
    if (-not (Test-Path -LiteralPath $Src)) { Block ("папки " + $Src + " нет — это общая среда конвейера (python, модели). Без неё ничего не запустится"); $srcOk = $false }
    else {
        foreach ($d in @("env", "envfast", "hf", "tools")) {
            if (Test-Path -LiteralPath (Join-Path $Src $d)) { Det ("есть " + (Join-Path $Src $d)) }
            elseif ($d -eq "tools") { Info ("папки " + (Join-Path $Src $d) + " нет — ffmpeg тогда должен находиться через PATH") }
            else { Block ("нет " + (Join-Path $Src $d)); $srcOk = $false }
        }
        $py1 = Join-Path (Join-Path $Src "env") "python.exe"
        $py2 = Join-Path (Join-Path (Join-Path $Src "envfast") "Scripts") "python.exe"
        $p1 = PyProbe $py1 @("torch", "numpy", "soundfile", "docx", "qwen_tts")
        if (-not $p1.ok) { Block ("python основной среды не запускается: " + $py1); $srcOk = $false }
        elseif ($p1.missing.Count -gt 0) { Need ("основная среда (python " + $p1.ver + "): не хватает пакетов " + ($p1.missing -join ", ") + " — пришлите отчёт в чат") }
        else { Ok ("основная среда " + (Join-Path $Src "env") + ": python " + $p1.ver + $(if (Test-Path -LiteralPath (Join-Path (Join-Path $Src "env") "conda-meta")) { " (окружение conda)" } else { "" }) + ", пакеты на месте") }
        if ($p1.pkgs) { Det ("пакеты основной среды: " + $p1.pkgs) }
        $p2 = PyProbe $py2 @("torch", "numpy", "soundfile", "faster_qwen3_tts", "ruaccent", "faster_whisper", "openpyxl", "silero_stress", "scipy")
        if (-not $p2.ok) { Block ("python быстрой среды не запускается: " + $py2); $srcOk = $false }
        else {
            $hard = @($p2.missing | Where-Object { @("ruaccent", "faster_whisper", "openpyxl", "silero_stress") -notcontains $_ })
            $soft = @($p2.missing | Where-Object { @("ruaccent", "faster_whisper", "openpyxl", "silero_stress") -contains $_ })
            if ($hard.Count -gt 0) { Need ("быстрая среда (python " + $p2.ver + "): не хватает пакетов " + ($hard -join ", ") + " — пришлите отчёт в чат") }
            else { Ok ("быстрая среда " + (Join-Path $Src "envfast") + ": python " + $p2.ver + $(if (Test-Path -LiteralPath (Join-Path (Join-Path $Src "envfast") "pyvenv.cfg")) { " (venv)" } else { "" }) + ", пакеты озвучки на месте") }
            if ($p2.pkgs) {
                Det ("пакеты быстрой среды: " + $p2.pkgs)
                $tv = ""; foreach ($kv in $p2.pkgs.Split(";")) { if ($kv -like "torch=*") { $tv = $kv.Substring(6) } }
                if ($tv -and $tv -notlike "*+cu*") { Need ("torch в быстрой среде — версия " + $tv + " без поддержки видеокарты (нет «+cu»). Похоже, пакеты обновляли (в PyCharm?). Пришлите отчёт в чат") }
                elseif ($tv) { Info ("torch " + $tv + " — с поддержкой видеокарты. Пакеты этой среды НЕ обновляйте (в PyCharm — стрелки ▲ в «Python Interpreter»): версии подобраны друг к другу") }
            }
            if ($soft.Count -gt 0) { Self ("пакеты " + ($soft -join ", ") + " go поставит сам при первом запуске (нужен интернет)") }
        }
        if (-not (Test-Path -LiteralPath $srcPy)) { Block ("нет исходника конвейера " + $srcPy); $srcOk = $false }
        else {
            $h = Sha1Norm $srcPy
            if ($h -eq $BASE_SHA) { Ok ("исходник конвейера цел: " + $srcPy + " — из него go каждый раз собирает рабочий файл книги") }
            else {
                $cands = @(Get-ChildItem -LiteralPath $Root -Filter "nox_audiobook.py*" -File -ErrorAction SilentlyContinue) +
                         @(Get-ChildItem -LiteralPath $Src -Filter "nox_audiobook.py.*" -File -ErrorAction SilentlyContinue)
                foreach ($c in $cands) { if (-not $pristine -and (Sha1Norm $c.FullName) -eq $BASE_SHA) { $pristine = $c.FullName } }
                if ($pristine) {
                    Need ("исходник конвейера " + $srcPy + " ИЗМЕНЁН (не тот, на котором проверены правки). Целая копия есть: " + $pristine + " — ключ -Fix вернёт её на место")
                    $script:fixes.Add(@{ kind = "restore"; from = $pristine; to = $srcPy; text = "вернуть целый исходник конвейера" })
                } else { Block ("исходник конвейера " + $srcPy + " ИЗМЕНЁН, целой копии рядом нет. Подготовка может не примениться — пришлите отчёт в чат") }
            }
        }
        $hub = Join-Path (Join-Path $Src "hf") "hub"
        $models = @(Get-ChildItem -LiteralPath $hub -Directory -Filter "models--*" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name -replace '^models--', '' -replace '--', '/' })
        if ($models.Count -eq 0) { Self ("моделей в " + $hub + " нет — go скачает их при первом запуске (несколько ГБ, нужен интернет)") }
        else {
            $hs = DirStat $hub
            if (@($models | Where-Object { $_ -like "*ru-stress*" }).Count -gt 0) { Ok ("модели лежат в " + $hub + " (" + (SizeText $hs.bytes) + ", моделей: " + $models.Count + "); модель озвучки с ударениями на месте") }
            else { Self ("модели лежат в " + $hub + " (" + (SizeText $hs.bytes) + "); модели озвучки с ударениями (ru-stress) среди них нет — go скачает её (несколько ГБ)") }
            foreach ($m in $models) { Det ("модель " + $m) }
            Info ("  папки env, envfast, hf в папке книги — это ссылки на " + $Src + ": модели и python общие для всех книг, второй раз не скачиваются")
        }
        $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
        $ff2 = @(Get-ChildItem -LiteralPath (Join-Path $Src "tools") -Recurse -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($ff -or $ff2.Count -gt 0) { Ok "ffmpeg найден" } else { Need "ffmpeg не найден ни в PATH, ни в tools — сборка частей и студия без него не работают; пришлите отчёт в чат" }
        $bank = Join-Path $Src "voicebank"
        if (Test-Path -LiteralPath $bank) { Info ("банк голосов: " + (DirStat $bank "*.wav").n + " образцов") } else { Info "банка голосов ещё нет — go создаст его сам" }
    }
} catch { Oops "общая среда" $_ }

# ---------------------------------------------------------------------------------------------- 3. папка книги
Head ("3. Папка книги " + $Root)
$rootOk = Test-Path -LiteralPath $Root
$nWav = 0; $wavBytes = [int64]0; $nVoices = 0; $stage = ""
$script:inWork = $false       # книга уже озвучивается нынешним конвейером и открывалась в студии: зачистка ей не нужна и вредна
try {
    if (-not $rootOk) { Info "папки книги ещё нет — go создаст её с нуля (рукопись возьмёт из Загрузок: kniga1.docx)" }
    else {
        foreach ($d in @("env", "envfast", "hf", "pip_cache", "tools")) {
            $lp = Join-Path $Root $d; $tp = Join-Path $Src $d
            if (-not (Test-Path -LiteralPath $lp)) {
                if (Test-Path -LiteralPath $tp) { Self ("ссылки " + $d + " нет — go создаст её") }
                continue
            }
            $it = Get-Item -LiteralPath $lp -Force
            if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $tg = "$(@($it.Target)[0])"
                if (-not $tg -or ($tg.TrimEnd('\') -ieq $tp.TrimEnd('\'))) { Det ("ссылка " + $d + " -> " + $tg) }
                else { Need ("ссылка " + $lp + " ведёт в " + $tg + ", а должна в " + $tp + " — пришлите отчёт в чат") }
            } else { Need ($lp + " — обычная папка, а должна быть ссылкой на " + $tp + " (так сделал другой скрипт?). Пришлите отчёт в чат") }
        }
        Ok "ссылки на общую среду (env, envfast, hf, tools) проверены"

        $bd = Join-Path $Root "book.docx"
        $canon = Join-Path $Downloads "kniga1.docx"        # go берёт рукопись именно под этим именем; копии «kniga1 (1).docx» он не смотрит
        $dd = $null; if ($leaf -eq "kniga1a") { $dd = Newest $Downloads "kniga1.docx" }
        $use = $null
        if ($leaf -eq "kniga1a" -and (Test-Path -LiteralPath $canon)) { $use = $canon } elseif (Test-Path -LiteralPath $bd) { $use = $bd }
        if (-not $use) { Block ("рукописи нет: ни kniga1.docx в Загрузках, ни " + $bd) }
        else {
            $fi = Get-Item -LiteralPath $use
            $same = ((Sha1File $use) -eq $DOCX_SHA)
            $t = "рукопись, которую возьмёт go: " + $use + "  (" + (SizeText $fi.Length) + ", " + (DateText $fi.LastWriteTime) + ")"
            if ($same) { Ok ($t + " — та же, что в комплекте") }
            else { Info ($t + " — отличается от рукописи из комплекта 06.11. Если вы правили текст сами, так и должно быть") }
            if (($use -ne $bd) -and (Test-Path -LiteralPath $bd) -and ((Sha1File $use) -ne (Sha1File $bd))) {
                Info ("в папке книги лежит другая рукопись (" + (DateText (Get-Item -LiteralPath $bd).LastWriteTime) + "); go заменит её той, что в Загрузках")
            }
            if ($dd -and ($dd.FullName -ne $use)) {
                if ((Sha1File $dd.FullName) -eq (Sha1File $use)) { Det ("копия «" + $dd.Name + "» в Загрузках — тот же файл, байт в байт") }
                else {
                    Need ("в Загрузках есть более свежая копия рукописи «" + $dd.Name + "» (" + (DateText $dd.LastWriteTime) + "), и она ОТЛИЧАЕТСЯ от той, что возьмёт go" + $(if ((Sha1File $dd.FullName) -eq $DOCX_SHA) { " (свежая — как раз рукопись из комплекта)" } else { "" }) + ". go берёт только файл с именем kniga1.docx. Если верна свежая — ключ -Fix даст ей это имя, прежняя сохранится рядом")
                    $script:fixes.Add(@{ kind = "copy"; from = $dd.FullName; to = $canon; text = "дать свежей рукописи имя kniga1.docx" })
                }
            }
        }
        $xl = Join-Path $Root "Настройки озвучки.xlsx"
        $okx = $false
        if (Test-Path -LiteralPath $xl) {
            try { $fs = [IO.File]::Open($xl, 'Open', 'Read', 'ReadWrite'); $b2 = New-Object byte[] 2; [void]$fs.Read($b2, 0, 2); $fs.Close(); $okx = ($b2[0] -eq 0x50 -and $b2[1] -eq 0x4B) } catch {}
            if ($okx) { Ok ("таблица настроек на месте (" + (DateText (Get-Item -LiteralPath $xl).LastWriteTime) + ") — ваши настройки сохранятся") }
            else { Need "таблица «Настройки озвучки.xlsx» не читается как файл Excel — рядом лежат её копии .bak_…: верните последнюю или удалите файл (go создаст новую)" }
            if (@(Get-ChildItem -LiteralPath $Root -Filter "~`$*.xlsx" -Force -ErrorAction SilentlyContinue).Count -gt 0) { Need "таблица настроек сейчас открыта в Excel — закройте её перед запуском go" }
        } else { Self "таблицы настроек нет — go создаст её с настройками по умолчанию" }
        $stj = Join-Path (Join-Path $Root "work") "settings.json"
        if (Test-Path -LiteralPath $stj) {
            try {
                $cfg = (Get-Content -LiteralPath $stj -Raw -Encoding UTF8) | ConvertFrom-Json
                $dname = "$($cfg.dest)"; if (-not $dname) { $dname = $(if ($leaf -eq "kniga1a") { "Posoh_izdanie3" } else { Split-Path $Root -Leaf }) }
                if ($leaf -eq "kniga1a" -and $dname -eq "Posoh_izdanie2") { $dname = "Posoh_izdanie3" }
                $dpath = $(if ($dname -match '^[A-Za-z]:\\') { $dname } else { Join-Path $Yandex $dname })
                Info ("настройки, с которыми пойдёт go (таблица, лист «Общие»; меняются там же, go читает таблицу при каждом запуске): воркеров " + $cfg.workers + ", длина куска текста " + $cfg.max_chars + " знаков, длина части " + $cfg.part_min + " мин, скорость речи " + [int][math]::Round(100 * [double]$cfg.tempo) + "%, части в " + $cfg.format + " " + $cfg.bitrate)
                Info ("  папка для Яндекс Диска: " + $dpath + " — строка «Папка на Яндекс Диске»: имя папки внутри Диска или полный путь к любой папке")
            } catch {}
        }

        $np = Join-Path $Root "nox_audiobook.py"
        if (-not (Test-Path -LiteralPath $np)) { Self "рабочего файла конвейера в папке книги нет — go соберёт его из исходника" }
        else {
            $stage = ""
            try { $m = Select-String -LiteralPath $np -Pattern '^NOX_STAGE = (\d+)' -Encoding UTF8 | Select-Object -First 1; if ($m) { $stage = $m.Matches[0].Groups[1].Value } } catch {}
            $sz = SizeText (Get-Item -LiteralPath $np).Length
            if ($stage -eq "21") { Ok ("рабочий файл конвейера собран нынешним комплектом (этап 21, " + $sz + ")") }
            elseif ($stage) { Self ("рабочий файл конвейера — от прежнего комплекта (этап " + $stage + ", " + $sz + "): go пересоберёт его из исходника") }
            elseif ((Sha1Norm $np) -eq $BASE_SHA) {
                Self ("рабочий файл конвейера ОТКАЧЕН к исходнику без единой правки (" + $sz + ") — так сделал clean_start2.ps1 («reverted to factory backup»). В таком виде книга озвучивается неверно. go пересоберёт файл из исходника сам")
                $script:clean.Add("конвейер был откачен к исходнику: всё, что озвучено после этого, сделано без правок")
            } else {
                Self ("рабочий файл конвейера изменён посторонним скриптом (" + $sz + ", правок комплекта в нём нет): go пересоберёт его из исходника сам")
                $script:clean.Add("файл конвейера правился посторонним скриптом")
            }
        }
        # копии файлов комплекта в папке книги: go заменяет их свежими из Загрузок
        $oldc = @()
        foreach ($f in @("nox_fast.py", "verify_k1a.py", "zvuk_k1a.py", "razmetka_k1a.py", "nastroyki_ozvuchki.py", "voicecheck_k1a.py", "voices_kniga1a.py",
                         "nox_part.py", "nox_stress.py", "nox_voices.py")) {
            $fp = Join-Path $Root $f
            if (Test-Path -LiteralPath $fp) { $v = KitVer $fp; if ($v -ne $KIT) { if ($v) { $oldc += ($f + " (" + $v + ")") } else { $oldc += ($f + " (без метки)") } } }
        }
        if ($oldc.Count -gt 0) { Self ("в папке книги старые или правленные копии: " + ($oldc -join ", ") + " — go заменит их свежими из Загрузок") }

        $alien = @();  foreach ($pat in $FOREIGN) { $alien += @(Get-ChildItem -LiteralPath $Root -Filter $pat -File -ErrorAction SilentlyContinue) }
        $alien = @($alien | Sort-Object FullName -Unique)
        $launch = @();  foreach ($pat in $LAUNCHERS) { $launch += @(Get-ChildItem -LiteralPath $Root -Filter $pat -File -ErrorAction SilentlyContinue) }
        $cbak = @();    foreach ($pat in $CODEBAK) { $cbak += @(Get-ChildItem -LiteralPath $Root -Filter $pat -File -ErrorAction SilentlyContinue) }
        $wbak = @(Get-ChildItem -LiteralPath (Join-Path $Root "work") -Filter "segments.csv.bak_*" -File -ErrorAction SilentlyContinue)
        if ($alien.Count -gt 0) {
            Need ("чужие скрипты в папке книги (не из комплекта): " + (($alien | ForEach-Object { $_.Name }) -join ", ") + ". НЕ ЗАПУСКАЙТЕ их: они откатывают конвейер и правят разбивку книги. Ключ -Fix уберёт их в папку _staroe")
            $script:clean.Add("в папке работали чужие скрипты (" + $alien.Count + ")")
        }
        if ($launch.Count -gt 0) { Need ("старые копии пусковых скриптов в папке книги: " + (($launch | ForEach-Object { $_.Name }) -join ", ") + ". Запускать их отсюда нельзя — рабочие лежат только в Загрузках. Ключ -Fix уберёт копии в _staroe") }
        $xbak = @(); if ($okx) { $xbak = @(Get-ChildItem -LiteralPath $Root -Filter "*.xlsx.bak_*" -File -ErrorAction SilentlyContinue) }
        if (($cbak.Count + $wbak.Count + $xbak.Count) -gt 0) { Info ("служебных копий .bak от чужих скриптов: " + ($cbak.Count + $wbak.Count + $xbak.Count) + " — не мешают; -Fix уберёт их в _staroe") }
        $lg = Join-Path $Root "logs"
        if (Test-Path -LiteralPath $lg -PathType Container) {
            $ls = DirStat $lg
            Info ("папка logs — журналы и копии, которые складывал другой помощник (файлов " + $ls.n + ", " + (SizeText $ls.bytes) + "); комплекту она не нужна. -Fix уберёт её в _staroe")
            $script:fixes.Add(@{ kind = "move"; from = $lg; text = "убрать в _staroe: папка logs" })
        }
        foreach ($f in @($alien + $launch + $cbak + $wbak + $xbak)) {
            $script:fixes.Add(@{ kind = "move"; from = $f.FullName; text = "убрать в _staroe: " + $f.Name })
            Det ($f.Name + "  " + (SizeText $f.Length) + "  " + (DateText $f.LastWriteTime))
        }
        $idea = Join-Path $Root ".idea"
        if (Test-Path -LiteralPath $idea) {
            $is = DirStat $idea
            $pyc = @(); try { $pyc = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like "pycharm*" }) } catch {}
            $recent = ($is.newest -and ((Get-Date) - $is.newest).TotalHours -lt 12)
            if ($pyc.Count -gt 0 -and $recent) {
                Need ("PyCharm сейчас запущен, а папка книги недавно открывалась в нём как проект (.idea, " + (DateText $is.newest) + "). Перед зачисткой и запуском go закройте этот проект (File - Close Project) или PyCharm целиком: он следит за папкой и может держать файлы, тогда зачистка молча пропустит их. Другой ваш проект в PyCharm не мешает. Для озвучки PyCharm не нужен")
            } else {
                Info ("папка книги открывалась как проект PyCharm (.idea, " + (DateText $is.newest) + ")" + $(if ($pyc.Count -gt 0) { "; PyCharm сейчас запущен — на время зачистки и go этот проект в нём лучше не открывать" } else { "; сейчас PyCharm не запущен" }) + ". Для озвучки PyCharm не нужен")
            }
        }
        # страховка от случайной публикации: папка книги (рукопись, озвучка, правки) не должна попасть в git
        $gitDir = Join-Path $Root ".git"; $gitIgn = Join-Path $Root ".gitignore"
        if (Test-Path -LiteralPath $gitDir) {
            Need "в папке книги создан git-репозиторий (папка .git). Рукопись, озвучку и правки публиковать нельзя: проверьте на github.com, что в репозитории их нет. Код для публикации лежит отдельно — в архиве nox_github_*.zip, публикуйте из его папки"
        } elseif (-not (Test-Path -LiteralPath $gitIgn)) {
            Info "папка книги не под git. Ключ -Fix положит в неё файл .gitignore со звёздочкой: если в PyCharm нажать «Share Project on GitHub», рукопись и озвучка в публикацию не попадут"
            $script:fixes.Add(@{ kind = "guard"; to = $gitIgn; text = "страховка от случайной публикации папки книги (.gitignore)" })
        } else {
            $giText = ""; try { $giText = [IO.File]::ReadAllText($gitIgn) } catch {}
            if ($giText -match '(?m)^\*\s*$') { Info "страховка от случайной публикации на месте: .gitignore в папке книги закрывает всё её содержимое" }
            else { Info "в папке книги свой .gitignore — сборщик его не меняет. Проверьте, что в нём закрыты рукопись, work, studio и gotovo" }
        }
        foreach ($d in @("logs", "diag", "parts", "parts_zvuk", "gotovo", "studio", "__pycache__")) {
            $dp = Join-Path $Root $d
            if (Test-Path -LiteralPath $dp) { $s = DirStat $dp; Det ("папка " + $d + ": файлов " + $s.n + ", " + (SizeText $s.bytes) + ", последнее изменение " + (DateText $s.newest)) }
        }
    }
} catch { Oops "папка книги" $_ }

# ---------------------------------------------------------------------------------------------- 4. рабочие данные
Head "4. Рабочие данные книги (work): разбивка, роли, голоса, озвучка"
try {
    $work = Join-Path $Root "work"
    if (-not (Test-Path -LiteralPath $work)) { Info "папки work ещё нет — go создаст её" }
    else {
        $seg = Join-Path $work "segments.csv"
        if (Test-Path -LiteralPath $seg) {
            $first = ""
            try { $first = "$(Get-Content -LiteralPath $seg -TotalCount 1 -Encoding UTF8)" } catch {}
            $rows = 0; try { $rows = @([IO.File]::ReadLines($seg)).Count - 1 } catch {}
            $want = "id;chapter;ch_title;para;kind;speaker;conf;pause_before_ms;text"
            $bom = HasBom $seg
            if ($bom -or ($first.TrimStart([char]0xFEFF) -ne $want)) {
                Self ("разбивка книги work\segments.csv ИСПОРЧЕНА (" + $(if ($bom) { "в начале файла лишняя метка BOM; " } else { "" }) + "заголовок: «" + $first + "»). go пересоздаст её из рукописи сам")
                $script:clean.Add("разбивку книги правил посторонний скрипт")
            } elseif (Test-Path -LiteralPath (Join-Path $work "prep.sig")) { Ok ("разбивка книги: " + $rows + " реплик (" + (DateText (Get-Item -LiteralPath $seg).LastWriteTime) + ")") }
            else { Info ("разбивка книги: " + $rows + " реплик (" + (DateText (Get-Item -LiteralPath $seg).LastWriteTime) + "), сделана прежним комплектом. Первый запуск нового go пересоздаст её из рукописи") }
        } else { Self "разбивки книги ещё нет — go сделает её из рукописи" }

        $rj = Join-Path $work "roles.json"
        if (Test-Path -LiteralPath $rj) {
            $rp = RolesProbe $rj
            if ($rp.n -gt 0 -and $rp.bom) {
                Need ("в начале work\roles.json лишняя метка BOM (её добавил другой скрипт): конвейер на таком файле остановится. Ключ -Fix уберёт метку, прежний файл останется рядом")
                $script:fixes.Add(@{ kind = "nobom"; from = $rj; text = "убрать метку BOM из work\roles.json" })
            } elseif ($rp.n -gt 0) {
                Ok ("роли: " + $rp.n + " (work\roles.json читается; пол, возраст и описания go берёт из таблицы настроек)")
                if (@($rp.twins).Count -gt 0) { Det ("имена, различающиеся только регистром (это разные роли со своими голосами, так и должно быть): " + (@($rp.twins) -join "; ")) }
            } elseif ($rp.how -eq "python") {
                Block ("work\roles.json повреждён" + $(if ($rp.err) { " (" + $rp.err + ")" } else { " (в нём нет ролей)" }) + " — конвейер на нём остановится. Приложите архив отчёта в чат: файл восстановим, образцы голосов при этом не пострадают")
            } else {
                Info ("роли (work\roles.json) сборщик проверить не смог" + $(if ($rp.err) { ": " + $rp.err } else { "" }) + ". Сам конвейер читает этот файл через python; если go остановится на разбивке книги — приложите архив отчёта в чат")
            }
        } else { Self "ролей ещё нет — go возьмёт их из общей среды и таблицы настроек" }

        $vd = Join-Path $work "voices"
        $vs = @(Get-ChildItem -LiteralPath $vd -Filter "*.wav" -File -ErrorAction SilentlyContinue)
        $nVoices = $vs.Count
        if ($nVoices -gt 0) {
            Ok ("образцы голосов ролей: " + $nVoices + " — остаются как есть при обновлении и при полной зачистке")
            foreach ($nm in @("narrator", "dzhek", "horvat", "Джек", "Хорват")) {
                $vf = @($vs | Where-Object { $_.BaseName -ieq $nm })
                if ($vf.Count -gt 0) { Info ("  голос «" + $nm + "»: " + (SizeText $vf[0].Length) + ", от " + (DateText $vf[0].LastWriteTime)) }
            }
            $late = @($vs | Where-Object { $_.LastWriteTime -ge [datetime]"2026-11-06 14:00" -and $_.LastWriteTime -lt [datetime]"2026-11-07 06:00" })
            if ($late.Count -gt 0) { Info ("  образцов, изменённых 06.11 после 14:00 (когда работали чужие скрипты): " + $late.Count + " — если голос роли покажется не тем, в студии есть «Голоса» → «Вернуть прежний голос»") }
            foreach ($v in $vs) { Det ($v.Name + "  " + (SizeText $v.Length) + "  " + (DateText $v.LastWriteTime)) }
        } else { Self "образцов голосов в work\voices нет — go возьмёт их из общей среды или подберёт из банка голосов" }

        $wd = Join-Path $work "wav"
        $w = DirStat $wd "*.wav"; $nWav = $w.n; $wavBytes = $w.bytes
        if ($nWav -gt 0) {
            $nTxt = (DirStat $wd "*.txt").n; $nOk = (DirStat $wd "*.ok").n
            $legacy = @(Get-ChildItem -LiteralPath $wd -Filter "*.wav" -File -ErrorAction SilentlyContinue | Where-Object { $_.BaseName -match '^\d{1,6}$' }).Count
            $script:inWork = ($stage -eq "21") -and (Test-Path -LiteralPath (Join-Path (Join-Path $Root "studio") "edits.json"))
            Info ($(if ($script:inWork) { "озвучка книги: " } else { "старая озвучка: " }) + $nWav + " записей, " + (SizeText $wavBytes) + ", с " + (DateText $w.oldest) + " по " + (DateText $w.newest) + "; с текстом рядом — " + $nTxt + ", проверено распознаванием — " + $nOk)
            if ($legacy -gt 0) { Info ("  из них " + $legacy + " записей со старыми именами по номеру (их делал откаченный конвейер) — нынешний конвейер их не берёт") }
            if ($script:inWork) { Info "  Книга в работе: записи сделаны нынешним конвейером, часть уже открывалась в студии. При обновлении они остаются на месте; полная зачистка удалила бы их — она здесь не нужна" }
            else { Info "  При обычном go запись берётся как есть, только если совпали текст, роль и образец голоса; остальные лежат без дела. Полная зачистка удаляет все" }
        } else { Info "озвученных записей нет — книга озвучится с нуля, по одной части за запуск" }

        foreach ($f in @("prep.sig", "run_mode.json", "part_status.json", "parts.json", "settings.json", "asr_active.txt", "failed.txt")) {
            $fp = Join-Path $work $f
            if (Test-Path -LiteralPath $fp) { $fi = Get-Item -LiteralPath $fp; Det ("work\" + $f + "  " + (SizeText $fi.Length) + "  " + (DateText $fi.LastWriteTime)) }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $work "prep.sig"))) { Info "первый запуск нового комплекта выполнит подготовку целиком (правки конвейера, словарь, разбивка рукописи): 5–10 минут" }
    }
} catch { Oops "рабочие данные" $_ }

# ---------------------------------------------------------------------------------------------- 5. Загрузки
Head ("5. Загрузки " + $Downloads + ": файлы комплекта и данные книги")
try {
    if (-not (Test-Path -LiteralPath $Downloads)) { Block ("папки Загрузок нет: " + $Downloads) }
    else {
        $bad = @()
        foreach ($f in $KITFILES) {
            $n = Newest $Downloads $f
            if (-not $n) { $bad += $f; Det ("нет " + $f); continue }
            $v = KitVer $n.FullName
            if ($v -ne $KIT) { $bad += $f; Det ($n.Name + " — версия " + $(if ($v) { $v } else { "без метки" }) + ", нужна " + $KIT) }
            else { Det ($n.Name + "  " + $v + "  " + (DateText $n.LastWriteTime)) }
        }
        $zipName = "kit_nox_" + $KIT + ".zip"
        $ka = FindKitArchive
        if ($bad.Count -eq 0) { Ok ("все 19 файлов комплекта " + $KIT + " на месте") }
        elseif ($ka -and -not $Fix) {
            Need ("файлов комплекта " + $KIT + " в Загрузках пока нет (" + $bad.Count + " из 19), но свежий комплект лежит " + $(if ($ka.kind -eq "zip") { "архивом " } else { "в папке " }) + $ka.item.Name + " — ключ -Fix сам разложит его по местам (прежние версии уйдут в Загрузки\_nox_old)")
            $script:fixes.Add(@{ kind = "kit"; from = $ka.item.FullName; text = "разложить комплект из " + $ka.item.Name })
        } else {
            Block ("в Загрузках нет свежего комплекта (не хватает " + $bad.Count + " из 19" + $(if ($bad.Count -lt 15) { ": " + ($bad -join ", ") } else { "" }) + "). Скачайте из последнего сообщения Claude архив " + $zipName + " — распаковывать не нужно, это сделает сборщик с ключом -Fix")
            $script:getKit.Add("архив " + $zipName + " (в нём все 19 файлов комплекта; распаковывать не нужно)")
        }
        foreach ($d in $DATA) {
            $n = Newest $Downloads $d.name
            $rc = Join-Path $Root $d.name                      # копия в папке книги (её положил туда другой скрипт)
            $wc = Join-Path (Join-Path $Root "work") $d.work   # рабочая копия конвейера
            if ($n) {
                $sha = Sha1File $n.FullName
                if ($sha -eq $d.sha) { Ok ($d.what + ": " + $n.Name + " — последняя версия") }
                else {
                    $good = $true
                    if ($d.name -like "speaker_overrides*") { $good = (@(Select-String -LiteralPath $n.FullName -Pattern "`t=`t" -SimpleMatch -Encoding UTF8).Count -ge 100) }
                    if ($good) { Info ($d.what + ": " + $n.Name + " отличается от версии комплекта (" + (DateText $n.LastWriteTime) + ") — если правили сами, так и должно быть") }
                    elseif ((Test-Path -LiteralPath $rc) -and (Sha1File $rc) -eq $d.sha) {
                        Need ($d.what + ": в Загрузках СТАРАЯ версия " + $n.Name + ", свежая лежит в папке книги — ключ -Fix положит её в Загрузки")
                        $script:fixes.Add(@{ kind = "copy"; from = $rc; to = (Join-Path $Downloads $d.name); text = "вернуть в Загрузки " + $d.name })
                    } else { Block ($d.what + ": в Загрузках СТАРАЯ версия " + $n.Name + " — скачайте " + $d.name + " из сообщения Claude"); $script:getKit.Add($d.name) }
                }
            } else {
                $from = $null
                if ((Test-Path -LiteralPath $rc) -and (Sha1File $rc) -eq $d.sha) { $from = $rc }
                elseif ((Test-Path -LiteralPath $wc) -and (Sha1File $wc) -eq $d.sha) { $from = $wc }
                if ($from) {
                    $t = $d.what + ": в Загрузках нет " + $d.name + ", целая копия есть в " + $from + " — ключ -Fix положит её в Загрузки"
                    if ($d.must) { Need $t } else { Info $t }
                    $script:fixes.Add(@{ kind = "copy"; from = $from; to = (Join-Path $Downloads $d.name); text = "вернуть в Загрузки " + $d.name })
                } elseif ($d.must) { Block ($d.what + ": нет " + $d.name + " ни в Загрузках, ни в папке книги — без него go остановится. Скачайте его из сообщения Claude"); $script:getKit.Add($d.name) }
                elseif (Test-Path -LiteralPath $wc) { Info ($d.what + ": в Загрузках нет " + $d.name + " — останется тот, что уже в работе (work\" + $d.work + ")") }
                else { Need ($d.what + ": нет " + $d.name + " — скачайте его из сообщения Claude"); $script:getKit.Add($d.name) }
            }
        }
        $zk = @(Get-ChildItem -LiteralPath $Downloads -Filter "Nox_*.zip" -File -ErrorAction SilentlyContinue).Count
        if ($zk -gt 0 -or (Test-Path -LiteralPath (Join-Path $Src "zvuk"))) { Info "набор звуков и музыки найден — звуковой слой будет доступен в студии (новый архив звукового комплекта студия распакует сама при запуске; прежний комплект останется на месте)" }
    }
} catch { Oops "Загрузки" $_ }

# ---------------------------------------------------------------------------------------------- 6. студия и Диск
Head "6. Студия и Яндекс Диск"
try {
    $sd = Join-Path $Root "studio"
    if (Test-Path -LiteralPath (Join-Path $sd "edits.json")) {
        $fx = 0
        try { $j = (Get-Content -LiteralPath (Join-Path $sd "fixed_parts.json") -Raw -Encoding UTF8) | ConvertFrom-Json; $fx = @($j.parts).Count } catch {}
        Ok ("студия уже работала с этой книгой: правки, дубли и проекты (" + $sd + ") не трогают ни go, ни зачистка; закреплено частей: " + $fx)
    } else { Info "студия с этой книгой ещё не работала — она откроется сама, когда go соберёт первую часть" }
    if (Test-Path -LiteralPath $Yandex) {
        $runs = @()
        foreach ($nm in @("Posoh_izdanie3", "Posoh_izdanie2")) {
            $yp = Join-Path $Yandex $nm
            if (Test-Path -LiteralPath $yp) { $s = DirStat $yp; $runs += ($nm + ": файлов " + $s.n + ", " + (SizeText $s.bytes)) }
        }
        if ($runs.Count -gt 0) { Info ("Яндекс Диск: " + ($runs -join "; ") + " — прежние запуски лежат каждый в своей папке, их никто не трогает; лишнее удалите сами") }
        else { Ok ("Яндекс Диск найден: " + $Yandex) }
    } else { Need ("папки Яндекс Диска нет (" + $Yandex + ") — части будут собираться только в папке книги; путь можно задать в таблице настроек") }
} catch { Oops "студия и Диск" $_ }

# ---------------------------------------------------------------------------------------------- исправления (-Fix)
$staroe = Join-Path $Root ("_staroe_" + $stamp)
$fixDone = 0
if ($Fix) {
    Head "ИСПРАВЛЕНИЯ (-Fix): ничего не удаляется"
    if ($script:fixes.Count -eq 0) { Say "  исправлять нечего" }
    foreach ($a in $script:fixes) {
        try {
            if ($a.kind -eq "copy") {
                if (-not (Test-Path -LiteralPath $a.to)) { Copy-Item -LiteralPath $a.from -Destination $a.to; Say ("  + скопировано: " + $a.from + " -> " + $a.to) "Green"; $fixDone++ }
                else {
                    $keep = $a.to + ".old_" + $stamp
                    Move-Item -LiteralPath $a.to -Destination $keep; Copy-Item -LiteralPath $a.from -Destination $a.to
                    Say ("  + заменено: " + $a.to + " (прежний сохранён как " + (Split-Path $keep -Leaf) + ")") "Green"; $fixDone++
                }
            } elseif ($a.kind -eq "restore") {
                $keep = $a.to + ".izmenen_" + $stamp
                Copy-Item -LiteralPath $a.to -Destination $keep
                $txt = [IO.File]::ReadAllText($a.from, [Text.Encoding]::UTF8)
                [IO.File]::WriteAllText($a.to, $txt, (New-Object Text.UTF8Encoding $false))
                if ((Sha1Norm $a.to) -eq $BASE_SHA) { Say ("  + исходник конвейера восстановлен из " + $a.from + " (изменённый сохранён как " + (Split-Path $keep -Leaf) + ")") "Green"; $fixDone++ }
                else { Say ("  ! исходник не восстановился — верните его из " + $keep + " и пришлите отчёт в чат") "Red" }
            } elseif ($a.kind -eq "nobom") {
                $keep = $a.from + ".s_metkoy_" + $stamp
                Copy-Item -LiteralPath $a.from -Destination $keep
                $txt = [IO.File]::ReadAllText($a.from, [Text.Encoding]::UTF8)
                [IO.File]::WriteAllText($a.from, $txt, (New-Object Text.UTF8Encoding $false))
                if (-not (HasBom $a.from)) { Say ("  + метка BOM убрана: " + $a.from + " (прежний файл: " + (Split-Path $keep -Leaf) + ")") "Green"; $fixDone++ }
                else { Say ("  ! метка BOM не убралась: " + $a.from) "Red" }
            } elseif ($a.kind -eq "guard") {
                if (-not (Test-Path -LiteralPath $a.to)) {
                    [IO.File]::WriteAllText($a.to, "# Nox: папка книги (рукопись, озвучка, правки студии) не публикуется. Код для публикации лежит отдельно.`r`n*`r`n", (New-Object Text.UTF8Encoding $false))
                    Say "  + страховка от случайной публикации: в папке книги создан .gitignore — в git из неё ничего не попадёт" "Green"; $fixDone++
                }
            } elseif ($a.kind -eq "move") {
                if (Test-Path -LiteralPath $a.from) {
                    New-Item -ItemType Directory -Force -Path $staroe | Out-Null
                    Move-Item -LiteralPath $a.from -Destination (Join-Path $staroe (Split-Path $a.from -Leaf))
                    Say ("  + убрано в " + (Split-Path $staroe -Leaf) + ": " + (Split-Path $a.from -Leaf)) "Green"; $fixDone++
                }
            }
        } catch { Say ("  ! не вышло (" + $a.text + "): " + $_.Exception.Message) "Red" }
    }
    # ярлыки на рабочем столе: запуск в чистом окне (без профиля, conda и PyCharm)
    if ($IsWin) {
        try {
            $ws = New-Object -ComObject WScript.Shell
            $desk = [Environment]::GetFolderPath("Desktop")
            $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
            foreach ($sc in @(@{ n = "Nox go.lnk"; a = (PsArgs "go_kniga1a*.ps1" $rootTail $true); d = "Nox: ozvuchit i sobrat sleduyushchuyu chast (go)" },
                             @{ n = "Nox proverka.lnk"; a = (PsArgs "sborshchik_k1a*.ps1" $rootTail $true); d = "Nox: proverka knigi (sborshchik)" })) {
                $lnk = $ws.CreateShortcut((Join-Path $desk $sc.n))
                $lnk.TargetPath = $psExe; $lnk.Arguments = $sc.a; $lnk.WorkingDirectory = $env:USERPROFILE; $lnk.Description = $sc.d
                $lnk.Save()
            }
            Say "  + ярлыки на рабочем столе: «Nox go» — озвучить и собрать следующую часть; «Nox proverka» — этот сборщик. Открываются в отдельном чистом окне" "Green"
        } catch { Say ("  ! ярлыки не созданы: " + $_.Exception.Message) "Red" }
    }
    if ($fixDone -gt 0) { Say ("  сделано исправлений: " + $fixDone + ". Запустите сборщик ещё раз без ключа — он покажет, что осталось.") "Green" }
}

# ---------------------------------------------------------------------------------------------- итог и порядок действий
Head "ИТОГ"
Say ("  в порядке: " + $script:cnt.ok + ";  починится само: " + $script:cnt.self + ";  нужно ваше действие: " + $script:cnt.need + ";  мешает запуску: " + $script:cnt.stop)
if ($script:cnt.stop -eq 0) { Say "  Обновлять можно: непоправимого не найдено. Всё, что сломал другой скрипт, пересобирается из целых исходников." "Green" }
else { Say "  Сначала уберите пункты [СТОП] — они перечислены выше; остальное go исправит сам." "Red" }
Say "  Что уцелеет в любом случае: образцы голосов ролей, роли, таблица настроек, рукопись, словарь, сценарий ролей, правки студии," 
Say "  готовые файлы на Яндекс Диске. Что пересобирается заново: рабочий файл конвейера, разбивка книги, служебные отметки."

Head "ЧТО ДЕЛАТЬ ПО ПОРЯДКУ"
Say "  Команды вставляйте в обычное окно PowerShell: Win+R, powershell, Enter. PyCharm и activate не нужны."
$k = 1
if ($script:getKit.Count -gt 0) {
    Say ("  " + $k + ") Скачайте в Загрузки из сообщения Claude: " + (($script:getKit | Select-Object -Unique) -join ", ")) "Yellow"; $k++
}
$pending = @($script:fixes | Where-Object { $_.kind -ne "move" -or (Test-Path -LiteralPath $_.from) })
if (-not $Fix -and $pending.Count -gt 0) {
    Say ("  " + $k + ") Безопасные исправления (" + $pending.Count + "; ничего не удаляется — копии и перенос в _staroe):") "Yellow"
    Say ("       " + (RunLine "sborshchik_k1a*.ps1" ($rootTail + " -Fix"))); $k++
}
if ($rootOk -and $script:inWork -and $script:clean.Count -eq 0) {
    Say ("  Зачистка НЕ нужна: книга в работе (" + $nWav + " записей нынешнего конвейера, правки студии). Не запускайте cleanup — он удалил бы черновую озвучку.") "Green"
    Say "  Обновление комплекта — это только запуск go: правки, дубли, проекты студии, записи и готовые файлы остаются на месте."
} elseif ($rootOk -and ($nWav -gt 0 -or $script:clean.Count -gt 0)) {
    Say ("  " + $k + ") Полная зачистка — советую: " + $(if ($script:clean.Count -gt 0) { ($script:clean | Select-Object -Unique) -join "; " } else { "старая озвучка сделана прежними версиями" }) + ".") "Yellow"
    Say ("       удалит: старую озвучку (" + $nWav + " записей, " + (SizeText $wavBytes) + "), собранные части, кэши, отметки, логи, откаченный файл конвейера;")
    Say ("       оставит: образцы голосов (" + $nVoices + "), роли, таблицу настроек, рукопись, словарь, сценарий ролей, студию, Яндекс Диск.")
    Say ("       " + (RunLine "cleanup_k1a*.ps1" $rootTail)); $k++
    Say "       Без зачистки тоже можно: go возьмёт прежние записи, где совпали текст, роль и голос, остальное озвучит заново."
}
Say ("  " + $k + ") Запуск:") "Yellow"
Say ("       " + (RunLine "go_kniga1a*.ps1" $rootTail))
if ($script:inWork -and $script:clean.Count -eq 0) {
    Say "       go сам остановит старые процессы, обновит конвейер и студию, сверит голоса и откроет «Нокс Студию» на той части,"
    Say "       что сейчас в работе: уже озвученное заново не озвучивается. Закрепили часть — следующий запуск берёт следующую."
} else {
    Say "       go сам остановит старые процессы, соберёт конвейер из исходника, разобьёт рукопись, проверит голоса, озвучит и соберёт"
    Say "       ПЕРВУЮ часть и откроет «Нокс Студию». Закрепили часть — следующий запуск берёт следующую."
}
Say "       После ключа -Fix то же самое делает ярлык «Nox go» на рабочем столе."
Say "       Редактор потом открывается ярлыком «Nox Studio» (его создаёт go при первом запуске) — озвучку для этого запускать не нужно."
Say "  Скрипты из папки книги не запускайте — только эти команды или ярлыки."

# ---------------------------------------------------------------------------------------------- отчёт и архив
$repFile = Join-Path $Downloads ("nox_otchet_" + $stamp + ".txt")
$zipFile = Join-Path $Downloads ("nox_otchet_" + $stamp + ".zip")
try {
    [IO.File]::WriteAllLines($repFile, $script:rep, (New-Object Text.UTF8Encoding $true))
    if (-not $NoZip) {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("nox_otchet_" + $stamp)
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        Copy-Item -LiteralPath $repFile -Destination (Join-Path $tmp "otchet.txt")
        $utf8 = New-Object Text.UTF8Encoding $true
        function ListDir([string]$dir, [string]$name, [bool]$deep) {
            if (-not (Test-Path -LiteralPath $dir)) { return }
            $opt = [IO.SearchOption]::TopDirectoryOnly; if ($deep) { $opt = [IO.SearchOption]::AllDirectories }
            $lines = New-Object System.Collections.Generic.List[string]
            try {
                foreach ($e in [IO.Directory]::EnumerateFileSystemEntries($dir, "*", $opt)) {
                    $fi = New-Object IO.FileInfo $e
                    $isDir = [bool]($fi.Attributes -band [IO.FileAttributes]::Directory)
                    $lines.Add(($fi.LastWriteTime.ToString("yyyy-MM-dd HH:mm") + "`t" + $(if ($isDir) { "<папка>" } else { "$($fi.Length)" }) + "`t" + $e.Substring($dir.Length).TrimStart('\', '/')))
                    if ($lines.Count -ge 4000) { $lines.Add("… (дальше не перечисляется)"); break }
                }
            } catch {}
            [IO.File]::WriteAllLines((Join-Path $tmp $name), $lines, $utf8)
        }
        ListDir $Root "spisok_kniga.txt" $false
        ListDir (Join-Path $Root "work") "spisok_work.txt" $false
        ListDir (Join-Path $Root "studio") "spisok_studio.txt" $false
        ListDir (Join-Path $Root "parts") "spisok_parts.txt" $false
        ListDir (Join-Path $Root "gotovo") "spisok_gotovo.txt" $false
        ListDir (Join-Path $Root "logs") "spisok_logs.txt" $true
        ListDir $Src "spisok_sreda.txt" $false
        try {
            $dn = @(Get-ChildItem -LiteralPath $Downloads -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^(go_|prep_|run_fast|cleanup_|nox_|verify_|voice|nastroyki|razmetka|zvuk_|speaker_|accent_|stress_|kniga|sborshchik|Nox_)' } |
                    ForEach-Object { $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm") + "`t" + $_.Length + "`t" + $_.Name + "`t" + (KitVer $_.FullName) })
            [IO.File]::WriteAllLines((Join-Path $tmp "spisok_zagruzki.txt"), [string[]]$dn, $utf8)
        } catch {}
        # мелкие служебные файлы целиком, журналы — хвостом
        foreach ($f in @("settings.json", "roles.json", "parts.json", "run_mode.json", "part_status.json", "prep.sig", "fast_args.json", "assembly.json", "voicecheck.txt", "audit_summary.txt")) {
            $fp = Join-Path (Join-Path $Root "work") $f
            if ((Test-Path -LiteralPath $fp) -and (Get-Item -LiteralPath $fp).Length -lt 3MB) { Copy-Item -LiteralPath $fp -Destination (Join-Path $tmp ("work_" + $f)) -ErrorAction SilentlyContinue }
        }
        $segf = Join-Path (Join-Path $Root "work") "segments.csv"
        if (Test-Path -LiteralPath $segf) { try { [IO.File]::WriteAllLines((Join-Path $tmp "work_segments_nachalo.txt"), [string[]]@(Get-Content -LiteralPath $segf -TotalCount 40 -Encoding UTF8), $utf8) } catch {} }
        $logs = @(Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -eq ".log" -or $_.Name -eq "log_fast.txt" }) +
                @(Get-ChildItem -LiteralPath (Join-Path $Root "work") -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @("failed.txt", "asr_log.txt", "asr_suspicious.txt", "part_check.txt") }) +
                @(Get-ChildItem -LiteralPath (Join-Path $Root "studio") -File -Filter "studio.log" -ErrorAction SilentlyContinue)
        foreach ($l in $logs) { try { [IO.File]::WriteAllLines((Join-Path $tmp ("hvost_" + $l.Name + ".txt")), [string[]]@(Get-Content -LiteralPath $l.FullName -Tail 200 -Encoding UTF8), $utf8) } catch {} }
        if (Test-Path -LiteralPath $zipFile) { Remove-Item -LiteralPath $zipFile -Force }
        Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zipFile -Force
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host ("Отчёт: " + $repFile) -ForegroundColor Green
    if (-not $NoZip -and (Test-Path -LiteralPath $zipFile)) {
        Write-Host ("Архив для чата (" + (SizeText (Get-Item -LiteralPath $zipFile).Length) + ", без звука и моделей): " + $zipFile) -ForegroundColor Green
        Write-Host "Если что-то в отчёте непонятно или есть пункты [СТОП] — приложите этот архив в чат."
        try { Set-Clipboard -Value $zipFile } catch {}
    }
} catch { Write-Host ("Отчёт не записан: " + $_.Exception.Message) -ForegroundColor Red }
Remove-Item -LiteralPath $script:probeFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $script:rolesProbe -Force -ErrorAction SilentlyContinue
