<#
.SYNOPSIS
    Starts claim-extractor's local review UI using the existing,
    unmodified entrypoint. Does not change application behavior.
#>

$ErrorActionPreference = "Stop"

$venvPython = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "$venvPython not found. Run setup-windows.ps1 first."
    exit 1
}

& $venvPython -m estimate_extractor ui @args
