[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Baseline,
    [Parameter(Mandatory)][string]$Candidate,
    [string]$ContextPath = (Join-Path $PSScriptRoot '..\..\context'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest')
)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') -ContextPath $ContextPath -CoreApiPath $CoreApiPath -ComputeApiPath $ComputeApiPath
if (-not $?) { throw 'Contract sync verification failed.' }
$python = Get-Command python -ErrorAction Stop
& $python.Source (Join-Path $PSScriptRoot 'compare-performance-baselines.py') --baseline $Baseline --candidate $Candidate
exit $LASTEXITCODE
