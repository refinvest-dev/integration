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
try {
    $arguments = @{
        DatabaseName = $DatabaseName
        TimeoutSeconds = $TimeoutSeconds
        Publication = $true
    }
    foreach ($name in @('TestRoot', 'ContextPath', 'CorePath', 'ComputePath', 'WebPath',
                         'EnvironmentFile', 'CorePort', 'ComputePort')) {
        if (Get-Variable -Name $name -ValueOnly) {
            $arguments[$name] = Get-Variable -Name $name -ValueOnly
        }
    }
    & (Join-Path $PSScriptRoot 'test-trading-normalization-runtime-contract.ps1') @arguments
    if (-not $?) { throw 'Publication runtime failed.' }
} catch {
    [Console]::Error.WriteLine("Publication E2E failed: $($_.Exception.Message)")
    exit 1
}
