# NOX 2026-11-18d. Windows PowerShell 5.1; ASCII source.
# Default: read-only plan. -Apply: move files to a checked same-volume quarantine.
# -NoBackup skips only the extra input-metadata copy; audio remains recoverable.
# -Restore <operation-id> -Apply restores a complete bundle after validation.
# -Recover -Apply rolls back an interrupted operation. No process is terminated.
param(
    [string]$Root = 'D:\kniga1a',
    [string]$NoxHome = 'D:\nox_audiobook',
    [switch]$NoBackup,
    [switch]$Apply,
    [switch]$Check,
    [string]$Restore = '',
    [switch]$Recover
)
$ErrorActionPreference = 'Stop'
if ($Apply -and $Check) { throw 'Choose either -Apply or -Check.' }
if ($Restore -and $Recover) { throw 'Choose either -Restore or -Recover.' }
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$helpers = @(
    (Join-Path $PSScriptRoot 'nox_maintenance.py'),
    (Join-Path $PSScriptRoot 'kit\nox_maintenance.py'),
    (Join-Path $NoxHome 'studio\nox_maintenance.py'),
    (Join-Path $Root 'nox_maintenance.py')
)
$helperPath = $null
foreach ($candidate in $helpers) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $helperPath = $candidate; break }
}
if (-not $helperPath) { throw 'The complete NOX 18d maintenance module was not found. Install the verified kit first.' }
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
$maintenanceArguments = @('-B', $helperPath, '--root', $Root, '--mode', 'edition')
if ($NoBackup) { $maintenanceArguments += '--no-backup' }
if ($Restore) { $maintenanceArguments += @('--restore', $Restore) }
if ($Recover) { $maintenanceArguments += '--recover' }
if ($Apply) { $maintenanceArguments += '--apply' }
& $pythonPath @maintenanceArguments
exit $LASTEXITCODE
