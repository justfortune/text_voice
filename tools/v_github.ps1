# NOX_GIT 2026-11-18d — проверенный комплект кода -> существующая ветка main.
# Без ключей только план. -NoPush готовит локальный коммит. Только -Push разрешает
# обычную fast-forward отправку. Рабочая книга, другая ветка и пользовательская
# среда не собираются в публикацию. История, подпись Git и origin не переписываются.
[CmdletBinding()]
param(
    [string]$Repo = 'https://github.com/justfortune/text_voice.git',
    [string]$Dir = 'D:\nox-audiobook',
    [string]$Book = 'D:\kniga1a',
    [string]$Src = 'D:\nox_audiobook',
    [string]$Downloads = '',
    [switch]$NoPush,
    [switch]$Push,
    [string]$Pack = '',
    [string]$Message = ''
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
$bookRoot = $Book

function Find-NoxPython {
    if ($env:NOX_TOOL_PY) {
        if (-not (Test-Path -LiteralPath $env:NOX_TOOL_PY -PathType Leaf)) { throw 'NOX_TOOL_PY указывает на отсутствующий Python.' }
        return $env:NOX_TOOL_PY
    }
    foreach ($base in @($bookRoot, $Src)) {
        foreach ($relative in @('envfast\Scripts\python.exe', 'envfast\python.exe', 'env\Scripts\python.exe', 'env\python.exe')) {
            $candidate = Join-Path $base $relative
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'Нужен Python 3.10 или новее; укажите NOX_TOOL_PY. Программы и пакеты не устанавливаются.'
}

function Find-NoxBundle {
    if ($Pack) {
        if (-not (Test-Path -LiteralPath (Join-Path $Pack 'release_manifest.json') -PathType Leaf)) {
            throw 'Pack должен указывать на распакованный полный комплект с release_manifest.json.'
        }
        return [IO.Path]::GetFullPath($Pack)
    }
    $local = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $local 'release_manifest.json') -PathType Leaf) { return $local }
    foreach ($record in @((Join-Path $bookRoot 'work\nox_release.json'), (Join-Path $Src 'studio\nox_release.json'))) {
        if (-not (Test-Path -LiteralPath $record -PathType Leaf)) { continue }
        $release = Get-Content -LiteralPath $record -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidate = [string]$release.bundle_dir
        if (-not $candidate -and $release.kit_dir) { $candidate = Split-Path -Parent ([string]$release.kit_dir) }
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'release_manifest.json') -PathType Leaf)) { return $candidate }
        throw ('Установленный комплект из ' + $record + ' недоступен. Укажите -Pack к целому распакованному архиву.')
    }
    throw 'Полный комплект не найден. Распакуйте архив 18d целиком и укажите -Pack. Отдельные файлы из Загрузок не смешиваются.'
}

try {
    if ($Push -and $NoPush) { throw 'Выберите один ключ: -Push или -NoPush.' }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git не найден.' }
    $bundle = Find-NoxBundle
    $pythonExe = Find-NoxPython
    $helper = Join-Path $bundle 'tools\nox_bundle_tools.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'Комплект неполон: нет tools\nox_bundle_tools.py.' }
    $action = 'plan'
    if ($NoPush) { $action = 'prepare' }
    if ($Push) { $action = 'push' }
    $arguments = @('-I', '-B', $helper, 'publish', '--bundle', $bundle, '--dir', $Dir, '--repo', $Repo,
                   '--book', $Book, '--home', $Src, '--action', $action)
    if ($Message) { $arguments += @('--message', $Message) }
    & $pythonExe @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('Публикация остановлена: ' + $_.Exception.Message) -ForegroundColor Red
    exit 2
}
