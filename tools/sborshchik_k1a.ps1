# NOX_SBOR 2026-11-18d — проверка полного комплекта и существующей книги.
# Без -Fix книга и установленный код не меняются. При отказе создаётся диагностический
# ZIP без аудио; -NoZip отключает его. Проверенные исправления выполняет только
# Update_NOX_18d.ps1 из целого комплекта с manifest и откатом.
[CmdletBinding()]
param(
    [string]$Root = 'D:\kniga1a',
    [string]$Src = 'D:\nox_audiobook',
    [string]$Downloads = '',
    [string]$Yandex = '',
    [switch]$Fix,
    [switch]$NoZip,
    [string]$Pack = ''
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
if (-not $Downloads) { $Downloads = Join-Path $env:USERPROFILE 'Downloads' }
if (-not $Yandex) { $Yandex = Join-Path $env:USERPROFILE 'YandexDisk' }
$bookRoot = $Root

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
    $bundle = Find-NoxBundle
    $pythonExe = Find-NoxPython
    $helper = Join-Path $bundle 'tools\nox_bundle_tools.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'Комплект неполон: нет tools\nox_bundle_tools.py.' }
    $arguments = @('-I', '-B', $helper, 'probe', '--bundle', $bundle, '--root', $Root, '--home', $Src,
                   '--downloads', $Downloads, '--output', $Downloads, '--yandex', $Yandex)
    if ($NoZip) { $arguments += '--no-zip' }
    & $pythonExe @arguments
    $result = $LASTEXITCODE
    if ($result -ne 0) { exit $result }
    if (-not $Fix) {
        Write-Host 'Для установки проверенного целого комплекта повторите с -Fix; проверка завершена.'
        exit 0
    }
    $updater = Join-Path $bundle 'Update_NOX_18d.ps1'
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) { throw 'Комплект неполон: нет Update_NOX_18d.ps1.' }
    $updateOptions = @{ Root = $Root; NoxHome = $Src; Downloads = $Downloads; Diagnostics = $Downloads }
    if ($NoZip) { $updateOptions['NoZip'] = $true }
    & $updater @updateOptions
    exit $LASTEXITCODE
} catch {
    Write-Host ('Проверка остановлена: ' + $_.Exception.Message) -ForegroundColor Red
    exit 2
}
