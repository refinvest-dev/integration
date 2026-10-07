[CmdletBinding()]
param(
    [string]$ContextPath = (Join-Path $PSScriptRoot "..\..\context"),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot "..\..\api\refinvest"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest"),
    [string]$WebPath = (Join-Path $PSScriptRoot "..\..\web\web"),
    [switch]$IncludeWeb
)

$ErrorActionPreference = "Stop"

function Assert-SameFile([string]$Source, [string]$Replica) {
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Contract source does not exist: $Source"
    }
    if (-not (Test-Path -LiteralPath $Replica)) {
        throw "Contract replica does not exist: $Replica"
    }

    # Git checkouts on Windows can legitimately differ only by LF/CRLF.
    # Compare normalized UTF-8 text so the check reports contract changes,
    # not checkout line-ending policy.
    $sourceText = (Get-Content -LiteralPath $Source -Raw).Replace("`r`n", "`n")
    $replicaText = (Get-Content -LiteralPath $Replica -Raw).Replace("`r`n", "`n")
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $sourceHash = [BitConverter]::ToString(
            $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($sourceText))
        ).Replace("-", "")
        $replicaHash = [BitConverter]::ToString(
            $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($replicaText))
        ).Replace("-", "")
    }
    finally {
        $sha256.Dispose()
    }
    if ($sourceHash -ne $replicaHash) {
        throw "Contract is out of sync: $Replica`nSource: $Source"
    }

    Write-Host "OK  $Replica"
}

Assert-SameFile `
    (Join-Path $ContextPath "openapi\core-api.yaml") `
    (Join-Path $CoreApiPath "openapi\core-api.yaml")
Assert-SameFile `
    (Join-Path $ContextPath "openapi\compute-api.yaml") `
    (Join-Path $CoreApiPath "openapi\compute-api.yaml")
Assert-SameFile `
    (Join-Path $ContextPath "openapi\compute-api.yaml") `
    (Join-Path $ComputeApiPath "openapi\compute-api.yaml")

if ($IncludeWeb) {
    Assert-SameFile `
        (Join-Path $ContextPath "openapi\core-api.yaml") `
        (Join-Path $WebPath "docs\api\open-api.yaml")
    Assert-SameFile `
        (Join-Path $ContextPath "openapi\compute-api.yaml") `
        (Join-Path $WebPath "docs\api\compute-api.yaml")
}

Write-Host "OpenAPI snapshots are synchronized with context."
