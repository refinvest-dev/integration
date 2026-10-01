[CmdletBinding()]
param(
    [string]$FixtureRoot = (Join-Path $PSScriptRoot "..\fixtures\trading-review\binance-usds-v1"),
    [string]$ContextPath = (Join-Path $PSScriptRoot "..\..\context"),
    [string]$Python = "python",
    [switch]$SkipTamperSelfTest
)

$ErrorActionPreference = "Stop"

$arguments = @(
    (Join-Path $PSScriptRoot "verify-trading-review-fixtures.py"),
    "--root", $FixtureRoot,
    "--context-path", $ContextPath
)
if (-not $SkipTamperSelfTest) {
    $arguments += "--self-test"
}

& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Trading Review fixture verification failed."
}
