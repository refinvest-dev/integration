[CmdletBinding()]
param(
    [ValidateSet("v1", "raw-v3", "all")]
    [string]$Dataset = "v1",
    [string]$FixtureRoot,
    [string]$ContextPath = (Join-Path $PSScriptRoot "..\..\context"),
    [string]$Python = "python",
    [switch]$SkipTamperSelfTest
)

$ErrorActionPreference = "Stop"

$datasetRoots = @()
if ($FixtureRoot) {
    $datasetRoots += $FixtureRoot
}
else {
    if ($Dataset -in @("v1", "all")) {
        $datasetRoots += (Join-Path $PSScriptRoot "..\fixtures\trading-review\binance-usds-v1")
    }
    if ($Dataset -in @("raw-v3", "all")) {
        $datasetRoots += (Join-Path $PSScriptRoot "..\fixtures\trading-review\binance-usds-v1-raw-v3")
    }
}

foreach ($root in $datasetRoots) {
    $arguments = @(
        (Join-Path $PSScriptRoot "verify-trading-review-fixtures.py"),
        "--root", $root,
        "--context-path", $ContextPath
    )
    if (-not $SkipTamperSelfTest) {
        $arguments += "--self-test"
    }

    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Trading Review fixture verification failed for $root."
    }
}
