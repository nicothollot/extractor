<#
.SYNOPSIS
One-shot setup for PV Extractor on Windows (PowerShell).

Guarantees a Python 3.12 environment even when your system Python is newer
(3.13 / 3.14 may lack prebuilt wheels for pymupdf / onnxruntime / rapidocr),
then runs scripts\bootstrap.py to create .venv and install everything.

.USAGE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1

Or just double-click setup.bat in the repo root. Idempotent; nothing here
touches your system Python (a fetched 3.12 is isolated under uv). A full
transcript is written to setup_log.txt for troubleshooting.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$LogPath = Join-Path $Root "setup_log.txt"
try { Start-Transcript -Path $LogPath -Force | Out-Null } catch {}

function Info($msg) { Write-Host $msg }

function Test-Py312([string]$Exe, [string[]]$PreArgs) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        & $Exe @PreArgs -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false } finally { $ErrorActionPreference = $prev }
}

$code = 1
try {
    Info "================================================"
    Info " PV Extractor - setup (Windows)"
    Info " Repo: $Root"
    Info "================================================"

    Info "Looking for a Python 3.12 interpreter ..."
    $PyExe = $null; $PyArgs = @()
    foreach ($cand in @(@{e = "py"; a = @("-3.12") }, @{e = "python"; a = @() }, @{e = "python3"; a = @() })) {
        Info "  trying: $($cand.e) $($cand.a -join ' ')"
        if (Test-Py312 $cand.e $cand.a) { $PyExe = $cand.e; $PyArgs = $cand.a; break }
    }

    if (-not $PyExe) {
        Info "No Python 3.12 on PATH. Installing an isolated CPython 3.12 via uv"
        Info "(this does NOT change your system Python) ..."
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Info "  downloading + installing uv ..."
            & powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
            $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
        }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "Could not install 'uv' automatically (no network, or the download was blocked). Install Python 3.12 yourself and re-run: 'winget install Python.Python.3.12' or https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
        }
        Info "  uv python install 3.12 ..."
        & uv python install 3.12
        if ($LASTEXITCODE -ne 0) { throw "uv failed to install Python 3.12 (exit $LASTEXITCODE)." }
        $PyExe = (& uv python find 3.12).Trim(); $PyArgs = @()
    }

    Info "Using Python: $PyExe $($PyArgs -join ' ')"
    & $PyExe @PyArgs --version 2>&1 | ForEach-Object { Info "  $_" }

    Info ""
    Info "Building .venv and installing dependencies (this can take a few minutes) ..."
    $Bootstrap = Join-Path $Root "scripts\bootstrap.py"
    & $PyExe @PyArgs $Bootstrap --with-gui
    if ($LASTEXITCODE -ne 0) { throw "bootstrap.py exited with code $LASTEXITCODE (see the messages above and $LogPath)." }

    Info ""
    Info "================================================"
    Info " Setup complete."
    Info "   Start the GUI: double-click 'Start PV Extractor.bat'"
    Info "                   or run  .venv\Scripts\pv-extractor.exe gui"
    Info "   Health check:  .venv\Scripts\pv-extractor.exe doctor"
    Info " Edit config.yaml to point pv_root at your documents share."
    Info "================================================"
    $code = 0
}
catch {
    Write-Host ""
    Write-Host "SETUP FAILED: $($_.Exception.Message)"
    Write-Host "Full log written to: $LogPath"
    Write-Host "If you're stuck, send that file."
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
exit $code
