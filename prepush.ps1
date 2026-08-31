$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python was not found. Create .venv or install Python 3.12."
    }
    $python = $pythonCommand.Source
}

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

Write-Host "[1/3] Compiling Python sources"
& $python -m py_compile saj_api.py fetcher.py backfill.py main.py sync_all.py fast_sync.py sync_customer_plants.py prod_smoke.py test_saj_optimization.py test_prod_smoke.py test_fast_sync.py test_fast_sync_api.py
Assert-LastExitCode "Python compilation"

Write-Host "[2/3] Running unit tests"
& $python -m unittest -v
Assert-LastExitCode "Unit tests"

Write-Host "[3/3] Checking patch whitespace"
& git diff --check
Assert-LastExitCode "git diff --check"

Write-Host "PRE-PUSH RESULT: PASS"
