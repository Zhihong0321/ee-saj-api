param(
    [string]$BaseUrl = "https://ee-saj-api-production.up.railway.app",
    [string]$DeviceSn = "R6M2053J2623E08431",
    [string]$ExpectedRevision = "",
    [string]$ReportPath = ".debug/prod-smoke-report.json"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

if (-not $ExpectedRevision) {
    $ExpectedRevision = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not determine the expected git revision."
    }
}

Write-Host "Waiting for production revision $ExpectedRevision"
& $python prod_smoke.py --base-url $BaseUrl --device-sn $DeviceSn `
    --expected-revision $ExpectedRevision --json-out $ReportPath
if ($LASTEXITCODE -ne 0) {
    throw "Production smoke test failed. See $ReportPath"
}

Write-Host "PRODUCTION RELEASE GATE: PASS"
