#requires -Version 5.1
<#
Nox Python Environment Inventory
PowerShell 5.1 compatible, READ-ONLY audit.
Discovers Python executables using PATH, py launcher, Conda, PEP 514 registry,
and bounded directory scans. Enumerates installed Python distributions for
each interpreter and runs pip check. Does not install/update/remove packages,
edit projects, or modify Git.
Only writes reports to the selected output directory and temporary probe files.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $HOME 'Downloads'),
    [int]$MaxDepth = 14,
    [int]$ProbeTimeoutSeconds = 120,
    [string[]]$AdditionalScanRoots = @()
)

$ErrorActionPreference = 'Continue'
$script:Candidates = New-Object System.Collections.ArrayList
$script:SeenPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
$script:ScanErrors = New-Object System.Collections.ArrayList
$script:Results = New-Object System.Collections.ArrayList
$script:PackageRows = New-Object System.Collections.ArrayList

function Add-ScanError([string]$Message) {
    [void]$script:ScanErrors.Add($Message)
}
function Normalize-Path([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $p = $Path.Trim().Trim('"')
    try {
        if (-not [System.IO.Path]::IsPathRooted($p)) { return $null }
        return [System.IO.Path]::GetFullPath($p)
    } catch { return $null }
}
function Add-Candidate([string]$Path, [string]$Source) {
    $p = Normalize-Path $Path
    if (-not $p) { return }
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return }
    if ($script:SeenPaths.Add($p)) {
        [void]$script:Candidates.Add([pscustomobject]@{
            Path = $p
            Sources = New-Object System.Collections.ArrayList
        })
    }
    $entry = $null
    foreach ($c in $script:Candidates) {
        if ([string]::Equals($c.Path, $p, [System.StringComparison]::OrdinalIgnoreCase)) {
            $entry = $c
            break
        }
    }
    if ($entry -and -not ($entry.Sources -contains $Source)) { [void]$entry.Sources.Add($Source) }
}
function Add-EnvRoot([string]$Root, [string]$Source) {
    if ([string]::IsNullOrWhiteSpace($Root)) { return }
    Add-Candidate (Join-Path $Root 'python.exe') $Source
}
function Write-Section([System.Collections.Generic.List[string]]$Lines, [string]$Title) {
    [void]$Lines.Add('')
    [void]$Lines.Add(('=' * 88))
    [void]$Lines.Add($Title)
    [void]$Lines.Add(('=' * 88))
}
function Get-DirectoriesToScan([string[]]$Roots) {
    $queue = New-Object 'System.Collections.Generic.Queue[object]'
    $visited = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $scanned = New-Object System.Collections.ArrayList
    $skipNames = @(
        '.git','.hg','.svn','node_modules','__pycache__','.mypy_cache','.pytest_cache',
        '.ruff_cache','.tox','.nox','Lib','Library','site-packages','include','share',
        'conda_pkgs','pkgs','pip_cache','.cache','cache','hf','hub','models',
        'work','voices','gotovo','tools','WindowsApps','Temp','tmp'
    )
    foreach ($root in $Roots) {
        $r = Normalize-Path $root
        if ($r -and (Test-Path -LiteralPath $r -PathType Container)) {
            $queue.Enqueue([pscustomobject]@{Path=$r;Depth=0})
        } elseif ($r) {
            Add-ScanError "Scan root absent: $r"
        }
    }
    while ($queue.Count -gt 0) {
        $node = $queue.Dequeue()
        $dir = $node.Path
        if (-not $visited.Add($dir)) { continue }
        [void]$scanned.Add($dir)
        # Do not traverse junctions/symlinks: aliases are independently reported by the junction checks.
        try {
            $children = Get-ChildItem -LiteralPath $dir -Directory -Force -ErrorAction Stop
        } catch {
            Add-ScanError "Cannot enumerate directory '$dir': $($_.Exception.Message)"
            continue
        }
        if ($node.Depth -ge $MaxDepth) { continue }
        foreach ($child in $children) {
            if ($skipNames -contains $child.Name) { continue }
            try {
                if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            } catch {}
            $queue.Enqueue([pscustomobject]@{Path=$child.FullName;Depth=($node.Depth+1)})
        }
    }
    return @($scanned)
}
function Invoke-InventoryProbe([string]$Exe, [string]$ProbePath, [int]$TimeoutSeconds, [string]$TempBase) {
    $stdout = "$TempBase.stdout.txt"
    $stderr = "$TempBase.stderr.txt"
    $process = $null
    $obj = [ordered]@{
        executable=$Exe; probe_ok=$false; exit_code=$null; timed_out=$false
        stderr=''; stdout=''; data=$null; error=''
    }
    try {
        $arg = '"' + $ProbePath + '"'
        $process = Start-Process -FilePath $Exe -ArgumentList $arg -PassThru -NoNewWindow `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr -ErrorAction Stop
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $obj.timed_out = $true
            try { $process.Kill() } catch {}
            $obj.error = "Probe timed out after $TimeoutSeconds seconds"
        } else {
            $process.Refresh()
            $obj.exit_code = $process.ExitCode
            $raw = ''
            if (Test-Path -LiteralPath $stdout) { $raw = Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue }
            if (Test-Path -LiteralPath $stderr) { $obj.stderr = Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue }
            $obj.stdout = $raw
            if ($raw) {
                try {
                    $obj.data = $raw | ConvertFrom-Json -ErrorAction Stop
                    $obj.probe_ok = $true
                } catch {
                    $obj.error = "Probe JSON parse failed: $($_.Exception.Message)"
                }
            } else {
                $obj.error = 'Probe produced no stdout'
            }
        }
    } catch {
        $obj.error = $_.Exception.Message
    } finally {
        foreach ($f in @($stdout,$stderr)) {
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    }
    return [pscustomobject]$obj
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$probePath = Join-Path $env:TEMP "nox_python_inventory_probe_$stamp.py"
$probeCode = @'
import os, sys, json, platform, struct, subprocess, importlib.util, importlib.metadata, traceback
data = {
    "executable": sys.executable,
    "version": sys.version.replace("\n", " "),
    "version_info": list(sys.version_info[:5]),
    "implementation": platform.python_implementation(),
    "architecture_bits": struct.calcsize("P") * 8,
    "platform": platform.platform(),
    "prefix": sys.prefix,
    "base_prefix": getattr(sys, "base_prefix", sys.prefix),
    "is_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    "conda_prefix_env_var": os.environ.get("CONDA_PREFIX"),
    "conda_default_env": os.environ.get("CONDA_DEFAULT_ENV"),
    "is_conda_env": os.path.isdir(os.path.join(sys.prefix, "conda-meta")),
    "conda_packages": [],
    "pip_version": None,
    "pip_check_exit": None,
    "pip_check_output": "",
    "modules": {},
    "torch": {"installed": False, "version": None, "cuda_build": None, "cuda_available": None,
              "device_count": None, "devices": [], "error": None},
    "packages": [],
    "metadata_error": None
}
modules = [
    "torch","torchaudio","faster_qwen3_tts","qwen_tts","transformers","huggingface_hub",
    "ruaccent","soundfile","librosa","numpy","scipy","pandas","openpyxl","docx",
    "gradio","faster_whisper","whisper","onnxruntime","num2words","sox","pytest",
    "playwright","selenium","grpc","kafka"
]
for name in modules:
    try:
        data["modules"][name] = "FOUND" if importlib.util.find_spec(name) else "MISSING"
    except Exception as e:
        data["modules"][name] = "ERROR:" + repr(e)
try:
    meta_dir = os.path.join(sys.prefix, "conda-meta")
    if os.path.isdir(meta_dir):
        for meta_file in os.listdir(meta_dir):
            if not meta_file.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(meta_dir, meta_file), "r", encoding="utf-8") as fh:
                    item = json.load(fh)
                data["conda_packages"].append({
                    "name": str(item.get("name", "")),
                    "version": str(item.get("version", "")),
                    "build": str(item.get("build", "")),
                    "channel": str(item.get("channel", item.get("schannel", "")))
                })
            except Exception:
                pass
    data["conda_packages"].sort(key=lambda x: (x["name"].lower(), x["version"]))
    dists = []
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata.get("Name") or "UNKNOWN"
            version = dist.version or ""
            dists.append({"name": name, "version": version})
        except Exception:
            pass
    dists.sort(key=lambda x: (x["name"].lower(), x["version"]))
    data["packages"] = dists
except Exception as e:
    data["metadata_error"] = repr(e)
try:
    import pip
    data["pip_version"] = getattr(pip, "__version__", "unknown")
except Exception as e:
    data["pip_version"] = "NOT_AVAILABLE: " + repr(e)
try:
    p = subprocess.run([sys.executable, "-m", "pip", "check"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=90)
    data["pip_check_exit"] = p.returncode
    data["pip_check_output"] = p.stdout.strip()
except Exception as e:
    data["pip_check_exit"] = -999
    data["pip_check_output"] = "PIP_CHECK_ERROR: " + repr(e)
if data["modules"].get("torch") == "FOUND":
    try:
        import torch
        data["torch"]["installed"] = True
        data["torch"]["version"] = str(torch.__version__)
        data["torch"]["cuda_build"] = str(torch.version.cuda) if torch.version.cuda else None
        data["torch"]["cuda_available"] = bool(torch.cuda.is_available())
        data["torch"]["device_count"] = int(torch.cuda.device_count())
        if data["torch"]["cuda_available"]:
            for i in range(data["torch"]["device_count"]):
                data["torch"]["devices"].append({
                    "index": i, "name": str(torch.cuda.get_device_name(i)),
                    "capability": list(torch.cuda.get_device_capability(i))
                })
    except Exception as e:
        data["torch"]["error"] = repr(e)
print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
'@

try {
    Set-Content -LiteralPath $probePath -Value $probeCode -Encoding UTF8
} catch {
    throw "Cannot create temporary Python probe: $($_.Exception.Message)"
}

Write-Host 'NOX PYTHON ENVIRONMENT INVENTORY'
Write-Host "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
Write-Host 'Read-only mode: no package installation/update/removal, no project edits, no Git mutations.'
Write-Host ''

# 1) PATH resolution
Write-Host 'Discovering Python launch points...'
foreach ($cmd in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
    if ($cmd.Source) { Add-Candidate $cmd.Source 'Get-Command python -All' }
}
if (Get-Command where.exe -ErrorAction SilentlyContinue) {
    foreach ($line in @(& where.exe python 2>$null)) { Add-Candidate ([string]$line) 'where.exe python' }
}
foreach ($line in @($env:PATH -split ';')) {
    $d = $line.Trim().Trim('"')
    if ($d -and (Test-Path -LiteralPath $d -PathType Container)) {
        Add-Candidate (Join-Path $d 'python.exe') "PATH directory: $d"
    }
}

# 2) Python launcher
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $pyOutput = @(& py.exe -0p 2>&1)
    foreach ($line in $pyOutput) {
        if ([string]$line -match '(?i)([A-Z]:\\.*?python(?:w)?\.exe)\s*$') {
            Add-Candidate $Matches[1] 'py -0p launcher'
        }
    }
}

# 3) Conda environments, including known Miniconda location
$condaCommands = New-Object System.Collections.ArrayList
foreach ($cmd in @(Get-Command conda -All -ErrorAction SilentlyContinue)) {
    if ($cmd.Source) { [void]$condaCommands.Add($cmd.Source) }
}
foreach ($candidate in @('E:\miniconda3\Scripts\conda.exe','E:\miniconda3\condabin\conda.bat','C:\ProgramData\miniconda3\Scripts\conda.exe')) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { [void]$condaCommands.Add($candidate) }
}
foreach ($conda in $condaCommands) {
    try {
        $raw = (& $conda env list --json 2>$null | Out-String)
        $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
        foreach ($prefix in $parsed.envs) { Add-EnvRoot ([string]$prefix) "Conda env list: $conda" }
    } catch {
        Add-ScanError "Conda discovery failed via '$conda': $($_.Exception.Message)"
    }
}

# 4) PEP 514 Python registry (installed CPython interpreters)
$registryRoots = @(
    'HKCU:\Software\Python\PythonCore',
    'HKLM:\Software\Python\PythonCore',
    'HKLM:\Software\WOW6432Node\Python\PythonCore'
)
foreach ($regRoot in $registryRoots) {
    if (-not (Test-Path -LiteralPath $regRoot)) { continue }
    try {
        foreach ($versionKey in Get-ChildItem -LiteralPath $regRoot -ErrorAction SilentlyContinue) {
            $installKey = Join-Path $versionKey.PSPath 'InstallPath'
            if (Test-Path -LiteralPath $installKey) {
                $rk = Get-Item -LiteralPath $installKey
                $exeValue = $rk.GetValue('ExecutablePath')
                $defaultValue = $rk.GetValue('')
                if ($exeValue) { Add-Candidate ([string]$exeValue) "PEP514 registry: $regRoot\$($versionKey.PSChildName)" }
                elseif ($defaultValue) { Add-EnvRoot ([string]$defaultValue) "PEP514 registry: $regRoot\$($versionKey.PSChildName)" }
            }
        }
    } catch { Add-ScanError "Registry scan failed at $regRoot : $($_.Exception.Message)" }
}

# 5) Known roots for the user's active and legacy projects + common Python locations.
$roots = New-Object System.Collections.ArrayList
$defaultRoots = @(
    'E:\NoxWorkspace',
    'E:\NoxWorkspace\NoxAudiobook_Books',
    'E:\miniconda3',
    'D:\kniga1a',
    'D:\kniga2a',
    'D:\kniga3a',
    'D:\nox_audiobook',
    (Join-Path $HOME 'AppData\Local\Programs\Python'),
    (Join-Path $HOME 'AppData\Local\pypoetry\Cache\virtualenvs'),
    (Join-Path $HOME '.virtualenvs'),
    (Join-Path $HOME 'Envs'),
    (Join-Path $HOME 'PycharmProjects'),
    (Join-Path $HOME 'PyCharmProjects'),
    (Join-Path $HOME 'Projects'),
    (Join-Path $HOME 'Documents'),
    (Join-Path $HOME 'Desktop')
)
foreach ($r in @($defaultRoots) + @($AdditionalScanRoots)) {
    if ($r -and -not ($roots -contains $r)) { [void]$roots.Add($r) }
}
# Typical machine-wide install directories, globbed only at their top level.
foreach ($glob in @('C:\Python*','C:\Program Files\Python*','C:\Program Files (x86)\Python*')) {
    foreach ($dir in @(Get-Item -Path $glob -ErrorAction SilentlyContinue)) {
        if ($dir.PSIsContainer) { [void]$roots.Add($dir.FullName) }
    }
}
$scanDirs = @(Get-DirectoriesToScan -Roots @($roots))
foreach ($dir in $scanDirs) {
    Add-Candidate (Join-Path $dir 'python.exe') "Directory scan: $dir"
}

# 6) PyCharm's saved SDK table, which may contain interpreters not otherwise active.
$jetbrainsRoot = Join-Path $env:APPDATA 'JetBrains'
if (Test-Path -LiteralPath $jetbrainsRoot -PathType Container) {
    foreach ($sdkFile in @(Get-ChildItem -LiteralPath $jetbrainsRoot -Filter 'jdk.table.xml' -File -Recurse -ErrorAction SilentlyContinue)) {
        try {
            [xml]$xml = Get-Content -LiteralPath $sdkFile.FullName -Raw
            foreach ($node in $xml.SelectNodes('//*[local-name()="homePath"]')) {
                $value = $node.GetAttribute('value')
                if ($value -match '(?i)python(?:w)?\.exe$') {
                    Add-Candidate $value "PyCharm SDK table: $($sdkFile.FullName)"
                } elseif ($value) {
                    Add-EnvRoot $value "PyCharm SDK table: $($sdkFile.FullName)"
                }
            }
        } catch { Add-ScanError "Cannot parse PyCharm SDK file $($sdkFile.FullName): $($_.Exception.Message)" }
    }
}

Write-Host ("Discovered candidate python.exe files: " + $script:Candidates.Count)
Write-Host ("Directories visited during bounded scans: " + $scanDirs.Count)
Write-Host ''

# Run a probe once per unique path.
$counter = 0
foreach ($candidate in $script:Candidates) {
    $counter++
    Write-Host ("[{0}/{1}] Inspecting {2}" -f $counter, $script:Candidates.Count, $candidate.Path)
    if ($candidate.Path -match '(?i)\\WindowsApps\\') {
        [void]$script:Results.Add([pscustomobject]@{
            path=$candidate.Path
            sources=(@($candidate.Sources) -join '; ')
            probe_ok=$false
            timed_out=$false
            probe_error='WindowsApps execution alias detected; deliberately not launched.'
            exit_code=$null
            version=''
            implementation=''
            architecture_bits=$null
            prefix=''
            base_prefix=''
            environment_type='WindowsApps shim'
            package_count=0
            pip_version=''
            pip_check_exit=$null
            pip_check_output=''
            torch_version=''
            cuda_available=$null
            cuda_build=''
            gpu_devices=''
            modules=$null
            packages=@()
            stderr=''
        })
        continue
    }
    $base = Join-Path $env:TEMP ("nox_pyinv_{0}_{1}" -f $stamp,$counter)
    $probe = Invoke-InventoryProbe -Exe $candidate.Path -ProbePath $probePath -TimeoutSeconds $ProbeTimeoutSeconds -TempBase $base
    $data = $probe.data
    $version = if ($data) { [string]$data.version } else { '' }
    $prefix = if ($data) { [string]$data.prefix } else { '' }
    $packages = if ($data -and $data.packages) { @($data.packages) } else { @() }
    $pkgCount = $packages.Count
    $condaPackages = if ($data -and $data.conda_packages) { @($data.conda_packages) } else { @() }
    $condaPkgCount = $condaPackages.Count
    $envKind = 'unknown'
    if ($data) {
        if ($data.is_conda_env) {
            $envKind = 'conda'
        } elseif ($data.is_venv) { $envKind = 'venv' }
        elseif ($prefix -match '(?i)WindowsApps') { $envKind = 'WindowsApps shim' }
        else { $envKind = 'system/base Python' }
    } elseif ($candidate.Path -match '(?i)WindowsApps') { $envKind = 'WindowsApps shim / launcher' }

    $pipCheckText = if ($data) { [string]$data.pip_check_output } else { '' }
    $pipCheckExit = if ($data) { $data.pip_check_exit } else { $null }
    $torchVersion = if ($data) { $data.torch.version } else { '' }
    $cuda = if ($data) { $data.torch.cuda_available } else { $null }

    $result = [pscustomobject]@{
        path=$candidate.Path
        sources=(@($candidate.Sources) -join '; ')
        probe_ok=[bool]$probe.probe_ok
        timed_out=[bool]$probe.timed_out
        probe_error=[string]$probe.error
        exit_code=$probe.exit_code
        version=$version
        implementation=$(if($data){$data.implementation}else{''})
        architecture_bits=$(if($data){$data.architecture_bits}else{$null})
        prefix=$prefix
        base_prefix=$(if($data){[string]$data.base_prefix}else{''})
        environment_type=$envKind
        package_count=$pkgCount
        conda_package_count=$condaPkgCount
        pip_version=$(if($data){[string]$data.pip_version}else{''})
        pip_check_exit=$pipCheckExit
        pip_check_output=$pipCheckText
        torch_version=$torchVersion
        cuda_available=$cuda
        cuda_build=$(if($data){[string]$data.torch.cuda_build}else{''})
        gpu_devices=$(if($data){(@($data.torch.devices | ForEach-Object {$_.name}) -join '; ')}else{''})
        modules=$(if($data){$data.modules}else{$null})
        packages=$packages
        conda_packages=$condaPackages
        stderr=[string]$probe.stderr
    }
    [void]$script:Results.Add($result)

    foreach ($pkg in $packages) {
        [void]$script:PackageRows.Add([pscustomobject]@{
            interpreter=$candidate.Path
            python_version=$version
            prefix=$prefix
            environment_type=$envKind
            package_source='python-metadata/pip'
            package_name=[string]$pkg.name
            package_version=[string]$pkg.version
            package_build=''
            package_channel=''
        })
    }
    foreach ($pkg in $condaPackages) {
        [void]$script:PackageRows.Add([pscustomobject]@{
            interpreter=$candidate.Path
            python_version=$version
            prefix=$prefix
            environment_type=$envKind
            package_source='conda-meta'
            package_name=[string]$pkg.name
            package_version=[string]$pkg.version
            package_build=[string]$pkg.build
            package_channel=[string]$pkg.channel
        })
    }
}

# Write comprehensive outputs outside all project trees.
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}
$txtPath = Join-Path $OutputDirectory 'Nox_Python_Inventory.txt'
$csvPath = Join-Path $OutputDirectory 'Nox_Python_Interpreters.csv'
$pkgCsvPath = Join-Path $OutputDirectory 'Nox_Python_Packages.csv'
$jsonPath = Join-Path $OutputDirectory 'Nox_Python_Inventory.json'

$lines = New-Object 'System.Collections.Generic.List[string]'
[void]$lines.Add('NOX PYTHON ENVIRONMENT INVENTORY')
[void]$lines.Add("Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
[void]$lines.Add("Computer: $env:COMPUTERNAME")
[void]$lines.Add("PowerShell: $($PSVersionTable.PSVersion)")
[void]$lines.Add("Read-only: no package install/update/removal; no project or Git changes.")
[void]$lines.Add("Scanned roots: $($roots -join '; ')")
[void]$lines.Add("Max scan depth: $MaxDepth")
[void]$lines.Add("Directories visited: $($scanDirs.Count)")
[void]$lines.Add("Unique Python executables discovered: $($script:Candidates.Count)")
[void]$lines.Add("Successfully probed: $(@($script:Results | Where-Object {$_.probe_ok}).Count)")
[void]$lines.Add("Packages inventoried: $($script:PackageRows.Count)")

Write-Section $lines 'INTERPRETERS SUMMARY'
foreach ($r in $script:Results) {
    [void]$lines.Add('')
    [void]$lines.Add("PYTHON: $($r.path)")
    [void]$lines.Add("  SOURCES: $($r.sources)")
    [void]$lines.Add("  PROBE_OK=$($r.probe_ok) TYPE=$($r.environment_type) VERSION=$($r.version)")
    [void]$lines.Add("  PREFIX=$($r.prefix)")
    [void]$lines.Add("  BASE_PREFIX=$($r.base_prefix)")
    [void]$lines.Add("  PYTHON_DISTRIBUTIONS=$($r.package_count) CONDA_PACKAGES=$($r.conda_package_count) PIP=$($r.pip_version)")
    [void]$lines.Add("  PIP_CHECK_EXIT=$($r.pip_check_exit) OUTPUT=$($r.pip_check_output)")
    [void]$lines.Add("  TORCH=$($r.torch_version) CUDA_AVAILABLE=$($r.cuda_available) CUDA_BUILD=$($r.cuda_build)")
    [void]$lines.Add("  GPU=$($r.gpu_devices)")
    if ($r.probe_error) { [void]$lines.Add("  PROBE_ERROR=$($r.probe_error)") }
    if ($r.stderr) { [void]$lines.Add("  STDERR=$($r.stderr.Trim())") }
    if ($r.modules) {
        [void]$lines.Add('  IMPORTANT_MODULES:')
        foreach ($name in @($r.modules.PSObject.Properties.Name | Sort-Object)) {
            [void]$lines.Add("    $name=$($r.modules.$name)")
        }
    }
}
Write-Section $lines 'FULL INSTALLED PACKAGE LIST BY INTERPRETER'
foreach ($r in $script:Results) {
    [void]$lines.Add('')
    [void]$lines.Add("### $($r.path)")
    if (-not $r.packages -and -not $r.conda_packages) {
        [void]$lines.Add('  [No package metadata returned]')
    } else {
        if ($r.packages) {
            [void]$lines.Add('  -- Python distributions / pip metadata --')
            foreach ($pkg in @($r.packages | Sort-Object name,version)) {
                [void]$lines.Add(("  {0}=={1}" -f $pkg.name,$pkg.version))
            }
        }
        if ($r.conda_packages) {
            [void]$lines.Add('  -- Conda package records --')
            foreach ($pkg in @($r.conda_packages | Sort-Object name,version)) {
                [void]$lines.Add(("  {0}=={1} [{2}]" -f $pkg.name,$pkg.version,$pkg.build))
            }
        }
    }
}
Write-Section $lines 'SCAN / DISCOVERY WARNINGS'
if ($script:ScanErrors.Count -eq 0) { [void]$lines.Add('No discovery warnings.') }
else { foreach ($err in $script:ScanErrors) { [void]$lines.Add($err) } }
Write-Section $lines 'END'
[void]$lines.Add('The report may include machine-specific paths and package names. Review before sharing publicly.')

try {
    [System.IO.File]::WriteAllLines($txtPath, $lines, ([System.Text.UTF8Encoding]::new($true)))
    $script:Results | Select-Object path,sources,probe_ok,timed_out,probe_error,version,implementation,architecture_bits,prefix,base_prefix,environment_type,package_count,conda_package_count,pip_version,pip_check_exit,pip_check_output,torch_version,cuda_available,cuda_build,gpu_devices |
        Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
    $script:PackageRows | Export-Csv -LiteralPath $pkgCsvPath -NoTypeInformation -Encoding UTF8
    $json = [pscustomobject]@{
        timestamp=(Get-Date -Format 'o')
        computer=$env:COMPUTERNAME
        scanned_roots=@($roots)
        max_depth=$MaxDepth
        directories_visited=$scanDirs.Count
        discovery_warnings=@($script:ScanErrors)
        interpreters=@($script:Results)
    } | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($jsonPath, $json, ([System.Text.UTF8Encoding]::new($true)))
} finally {
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
}
Write-Host ''
Write-Host 'INVENTORY COMPLETE' -ForegroundColor Green
Write-Host "Interpreters report: $txtPath"
Write-Host "Interpreters CSV:    $csvPath"
Write-Host "Packages CSV:        $pkgCsvPath"
Write-Host "Full JSON:           $jsonPath"
Write-Host "Candidates: $($script:Candidates.Count); probed: $(@($script:Results | Where-Object {$_.probe_ok}).Count); package rows: $($script:PackageRows.Count)"
Write-Host 'No packages were installed, updated, or removed.'