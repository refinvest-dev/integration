[CmdletBinding()]
param(
    [string]$TestRoot,
    [string]$ContextPath,
    [string]$CorePath,
    [string]$ComputePath,
    [string]$WebPath,
    [string]$EnvironmentFile,
    [int]$CorePort,
    [int]$ComputePort,
    [string]$DatabaseName = 'refinvest_integration',
    [int]$TimeoutSeconds = 180
)
$ErrorActionPreference = 'Stop'
if (-not $TestRoot) { $TestRoot = Join-Path $PSScriptRoot '..' }
if (-not $ContextPath) { $ContextPath = Join-Path $TestRoot '..\context' }
if (-not $CorePath) { $CorePath = Join-Path $TestRoot '..\api\refinvest' }
if (-not $ComputePath) { $ComputePath = Join-Path $TestRoot '..\compute-api\refinvest' }
if (-not $WebPath) { $WebPath = Join-Path $TestRoot '..\web\web' }
if (-not $EnvironmentFile) { $EnvironmentFile = Join-Path $TestRoot '.env.local' }

try {
    & (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') -ContextPath $ContextPath `
        -CoreApiPath $CorePath -ComputeApiPath $ComputePath -WebPath $WebPath -IncludeWeb
    if (-not $?) { throw 'Contract preflight failed.' }

    Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
    $python = Get-IntegrationPython -ComputeApiPath $ComputePath
    & $python -B -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from trading_normalization_result_verifier import self_test, SPEC; self_test(Path(sys.argv[2])); print(SPEC)' `
        $PSScriptRoot $ContextPath
    if ($LASTEXITCODE -ne 0) { throw 'Fixed result vector self-test failed.' }

    $arguments = @{
        TestRoot = $TestRoot; ContextPath = $ContextPath; CorePath = $CorePath
        ComputePath = $ComputePath; WebPath = $WebPath; EnvironmentFile = $EnvironmentFile
        DatabaseName = $DatabaseName; TimeoutSeconds = $TimeoutSeconds
    }
    if ($CorePort) { $arguments.CorePort = $CorePort }
    if ($ComputePort) { $arguments.ComputePort = $ComputePort }
    Write-Host 'Running Runtime Identity Contract Test gate.'
    & (Join-Path $PSScriptRoot 'test-trading-normalization-runtime-contract.ps1') @arguments
    if (-not $?) { throw 'Runtime identity gate failed.' }
    Write-Host 'Running Result Integrity Runtime Test.'
    $env:TRADING_RUNTIME_IDENTITY_GATE = 'PASS'
    try {
        & (Join-Path $PSScriptRoot 'test-trading-normalization-runtime-contract.ps1') @arguments -ResultIntegrity
        if (-not $?) { throw 'Result integrity runtime failed.' }
    } finally {
        Remove-Item Env:TRADING_RUNTIME_IDENTITY_GATE -ErrorAction SilentlyContinue
    }
} catch {
    [Console]::Error.WriteLine("Result integrity test failed: $($_.Exception.Message)")
    exit 1
}
