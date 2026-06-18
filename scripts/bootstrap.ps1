<#
.SYNOPSIS
First-run setup for PV Extractor (PowerShell mirror of bootstrap.py):
find Python >= 3.12, create .venv, install the package editable.

.USAGE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 [-WithGui]

Idempotent: re-running detects the existing .venv and skips completed steps.
Every failure prints a remediation message and exits non-zero.

Native commands are wrapped in Invoke-Native: Windows PowerShell 5.1 converts
redirected stderr lines into a terminating NativeCommandError when
$ErrorActionPreference is Stop, which would otherwise kill the script on the
EXPECTED ImportError from the "is the package installed yet?" probe.
#>
param([switch]$WithGui)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not ($env:OS -eq "Windows_NT")) {
    $VenvPython = Join-Path $VenvDir "bin/python"
}
$ConfigPath = Join-Path $ProjectRoot "config.yaml"

function Invoke-Native {
    # Run a native command with stdout/stderr captured; return its exit code.
    # Relaxes $ErrorActionPreference around the call so PS 5.1 does not turn
    # stderr output into a terminating error.
    param([string]$Exe, [string[]]$Arguments, [switch]$ShowOutputOnError)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
        if ($code -ne 0 -and $ShowOutputOnError -and $out) {
            $out | ForEach-Object { Write-Host "  $_" }
        }
        return $code
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Find-Python {
    # Returns @{ Exe = ...; Args = @(...) } for the first interpreter >= 3.12.
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "python3"; Args = @() },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $check = @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)")
        if ((Invoke-Native $c.Exe ($c.Args + $check)) -eq 0) { return $c }
    }
    return $null
}

function Read-InstallMissingDeps {
    # first_run.install_missing_deps via plain line scan (no YAML lib).
    if (-not (Test-Path $ConfigPath)) { return $true }
    foreach ($line in Get-Content $ConfigPath) {
        $clean = ($line -split "#", 2)[0].Trim()
        if ($clean -match "^install_missing_deps:\s*(.+)$") {
            $value = $Matches[1].Trim().Trim("'").Trim('"').ToLower()
            return -not (@("false", "no", "off", "0") -contains $value)
        }
    }
    return $true
}

# --- GUI prerequisites -------------------------------------------------------
if ($WithGui) {
    Write-Host "Checking Node.js / npm for the GUI extra ..."
    foreach ($tool in @("node", "npm")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Write-Host "Node.js and npm are required for -WithGui but '$tool' was not found on PATH."
            Write-Host "Install Node.js 20+ from https://nodejs.org, or:"
            Write-Host "  winget install OpenJS.NodeJS.LTS"
            Write-Host "Then re-run: scripts\bootstrap.ps1 -WithGui"
            exit 2
        }
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $ver = & $tool --version 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -ne 0) {
            Write-Host "'$tool --version' failed. Reinstall Node.js from https://nodejs.org and re-run."
            exit 2
        }
        Write-Host "  $tool $ver"
    }
}

# --- Python ------------------------------------------------------------------
$Py = Find-Python
if ($null -eq $Py) {
    Write-Host "Python >= 3.12 was not found (tried: py -3.12, python3, python)."
    Write-Host "Install it and re-run:"
    Write-Host "  winget install Python.Python.3.12"
    Write-Host "  or download from https://www.python.org/downloads/"
    exit 1
}
Write-Host "Using $($Py.Exe) $($Py.Args -join ' ')"

# --- config.yaml (machine-specific; git-ignored, seeded from the template) ---
$ConfigTemplatePath = Join-Path $ProjectRoot "config.example.yaml"
if ((-not (Test-Path $ConfigPath)) -and (Test-Path $ConfigTemplatePath)) {
    Copy-Item $ConfigTemplatePath $ConfigPath
    Write-Host "Seeded config.yaml from config.example.yaml — edit it for this machine (set output_dir to a local writable folder)."
}

# --- venv --------------------------------------------------------------------
if (Test-Path $VenvPython) {
    Write-Host "Found existing virtualenv: $VenvDir"
} else {
    Write-Host "Creating virtualenv at $VenvDir ..."
    $VenvExit = Invoke-Native $Py.Exe ($Py.Args + @("-m", "venv", $VenvDir)) -ShowOutputOnError
    if ($VenvExit -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Host "venv creation failed. Try manually:"
        Write-Host "  $($Py.Exe) $($Py.Args -join ' ') -m venv `"$VenvDir`""
        exit 1
    }
}

# --- editable install --------------------------------------------------------
$Extras = if ($WithGui) { ".[dev,gui]" } else { ".[dev]" }

# Completeness probe: EVERY runtime dependency must resolve, not just the
# package — a venv left over from an earlier phase must trigger a re-install.
# Keep the module list in sync with pyproject.toml / REQUIRED_IMPORTS in
# bootstrap.py. find_spec executes no module code, so the probe is fast.
$Probe = 'import importlib.util, sys; names = "pv_extractor fitz rapidfuzz pydantic typer dateutil rich yaml openpyxl pdfplumber docx pptx rapidocr onnxruntime".split(); missing = [n for n in names if importlib.util.find_spec(n) is None]; print(" ".join(missing)); raise SystemExit(1 if missing else 0)'

if ((Invoke-Native $VenvPython @("-c", $Probe)) -ne 0) {
    if (-not (Read-InstallMissingDeps)) {
        Write-Host "Dependencies are missing and first_run.install_missing_deps is false in config.yaml."
        Write-Host "Either install manually:"
        Write-Host "  cd `"$ProjectRoot`""
        Write-Host "  `"$VenvPython`" -m pip install -e `"$Extras`""
        Write-Host "or set first_run.install_missing_deps: true and re-run scripts\bootstrap.ps1"
        exit 1
    }
    Write-Host "Installing pv-extractor (editable, $Extras) - this may take a few minutes ..."
    Push-Location $ProjectRoot
    $InstallExit = Invoke-Native $VenvPython @("-m", "pip", "install", "--quiet", "-e", $Extras) -ShowOutputOnError
    Pop-Location
    if ($InstallExit -ne 0) {
        Write-Host "pip install failed (exit $InstallExit). Retry manually:"
        Write-Host "  cd `"$ProjectRoot`""
        Write-Host "  `"$VenvPython`" -m pip install -e `"$Extras`""
        exit 1
    }
} else {
    Write-Host "pv_extractor and all dependencies already installed in .venv (skipping install)."
}

# --- verify + next steps -----------------------------------------------------
if ((Invoke-Native $VenvPython @("-c", $Probe) -ShowOutputOnError) -ne 0) {
    Write-Host "Dependencies still missing after install (listed above). Retry manually:"
    Write-Host "  cd `"$ProjectRoot`""
    Write-Host "  `"$VenvPython`" -m pip install -e `"$Extras`""
    exit 1
}

$Cli = Join-Path (Split-Path -Parent $VenvPython) "pv-extractor.exe"
Write-Host ""
Write-Host "Bootstrap complete. Next steps:"
Write-Host "  Run the tests:   `"$VenvPython`" -m pytest -q"
Write-Host "  Try the CLI:     `"$Cli`" locate --client `"Angelo Gordon`" --deal `"Accell`" --period `"2025-01-31`" --doc-type valuation_memo"
exit 0
