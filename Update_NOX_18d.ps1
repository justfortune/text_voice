# NOX 2026-11-18d. Compatible with Windows PowerShell 5.1.
# Default: validate and apply a code-only update. -Check: validate only.
param(
    [string]$Root = 'D:\kniga1a',
    [string]$NoxHome = 'D:\nox_audiobook',
    [switch]$Check,
    [string]$Rollback = '',
    [string]$Diagnostics = '',
    [switch]$NoZip,
    [string]$Downloads = ''
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$bundleDir = $PSScriptRoot
$updateScript = Join-Path $bundleDir 'kit\nox_update.py'
if (-not (Test-Path -LiteralPath $updateScript -PathType Leaf)) {
    throw 'Unpack the complete NOX 18d archive before running this file.'
}
# Parse every shipped PowerShell file with the user's own PS 5.1/7 parser.
# This check performs no script execution and runs before the first update write.
foreach ($scriptFile in Get-ChildItem -LiteralPath $bundleDir -Filter '*.ps1' -File -Recurse) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count -gt 0) {
        throw ('PowerShell syntax check failed: ' + $scriptFile.FullName + ' : ' + ($parseErrors.Message -join '; '))
    }
}
$candidates = @(
    (Join-Path $Root 'envfast\Scripts\python.exe'),
    (Join-Path $NoxHome 'envfast\Scripts\python.exe'),
    (Join-Path $Root 'env\python.exe'),
    (Join-Path $NoxHome 'env\python.exe')
)
$pythonPath = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        & $candidate -c 'import sys; assert sys.version_info >= (3, 10)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonPath = $candidate; break }
    }
}
if (-not $pythonPath) { throw 'An existing working Python 3.10+ environment was not found. No packages were changed.' }
if (-not $Downloads) { $Downloads = Join-Path $env:USERPROFILE 'Downloads' }
if (-not $Diagnostics) { $Diagnostics = $Downloads }
$updateArguments = @('-B', $updateScript, '--root', $Root, '--home', $NoxHome, '--bundle', $bundleDir,
    '--output', $Diagnostics, '--downloads', $Downloads)
if ($Rollback) { $updateArguments += @('--rollback', $Rollback) }
elseif (-not $Check) { $updateArguments += '--apply' }
if ($NoZip) { $updateArguments += '--no-diagnostics' }
& $pythonPath @updateArguments
exit $LASTEXITCODE
