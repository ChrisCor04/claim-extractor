<#
.SYNOPSIS
    Windows-only dependency setup for claim-extractor.

.DESCRIPTION
    Creates .venv (if missing) and installs requirements-windows.txt plus
    the repo package itself (editable). That is all it does.

    It does NOT: change application configuration or source files, touch
    Xactimate calibration, launch Xactimate, run a claim, or install/modify
    Xactimate itself. Tesseract OCR is a separate, non-pip, external
    install -- this script only prints instructions for it, never installs
    it automatically.
#>

$ErrorActionPreference = "Stop"

# 1. Verify Windows -- the Xactimate automation dependencies (pywin32,
#    comtypes) are Windows-only.
if ($env:OS -ne "Windows_NT") {
    Write-Error "This script is Windows-only (the app's Xactimate automation requires pywin32/comtypes)."
    exit 1
}

# 2. Verify a Python 3.11+ interpreter exists (pyproject.toml requires >=3.11).
$pythonExe = $null
$pythonArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($ver in @("3.13", "3.12", "3.11")) {
        & py "-$ver" --version 2>$null 1>$null
        if ($LASTEXITCODE -eq 0) {
            $pythonExe = "py"
            $pythonArgs = @("-$ver")
            break
        }
    }
}
if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $verText = & python --version 2>&1
    if ($verText -match "Python 3\.(1[1-9]|[2-9]\d)") {
        $pythonExe = "python"
        $pythonArgs = @()
    }
}
if (-not $pythonExe) {
    Write-Error "No Python 3.11+ interpreter found. Install Python 3.11 or newer from python.org and re-run this script."
    exit 1
}
Write-Host "Using interpreter: $pythonExe $($pythonArgs -join ' ')"

# 3. Create .venv if it doesn't already exist. Never recreates an existing one.
if (-not (Test-Path ".venv")) {
    & $pythonExe @pythonArgs -m venv .venv
    Write-Host "Created .venv"
} else {
    Write-Host ".venv already exists -- reusing it"
}

$venvPython = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Expected $venvPython after venv creation but it is missing."
    exit 1
}

# 4. Install dependencies and the repo package (editable) into .venv only.
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements-windows.txt
& $venvPython -m pip install -e .

# 5. Report next steps. This script does not perform any of them.
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps (not performed by this script):"
Write-Host "  1. Install Tesseract OCR (required for Xactimate automation; not installable via pip):"
Write-Host "       https://github.com/UB-Mannheim/tesseract/wiki"
Write-Host "     Ensure tesseract.exe is on PATH, or set the TESSERACT_CMD environment variable to its full path."
Write-Host "  2. Start the app:  .\.venv\Scripts\python.exe -m estimate_extractor ui"
Write-Host "     (or run start-windows.ps1)"
Write-Host "  3. With Xactimate open on this machine, calibrate it from within the app before running Fast Grouped."
Write-Host "     Calibration is per-machine and is not part of this repository."
