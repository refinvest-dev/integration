[CmdletBinding()]
param(
    [string]$ContextPath,
    [string]$CoreApiPath,
    [string]$ComputeApiPath,
    [string]$WebPath,
    [switch]$IncludeWeb
)

$ErrorActionPreference = "Stop"
if (-not $ContextPath) { $ContextPath = Join-Path $PSScriptRoot '..\..\context' }
if (-not $CoreApiPath) { $CoreApiPath = Join-Path $PSScriptRoot '..\..\api\refinvest' }
if (-not $ComputeApiPath) { $ComputeApiPath = Join-Path $PSScriptRoot '..\..\compute-api\refinvest' }
if (-not $WebPath) { $WebPath = Join-Path $PSScriptRoot '..\..\web\web' }

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

# The identity vector is owned by Context. Compare fixture bytes, not a second oracle.
function Assert-SameBytes([string]$Source, [string]$Replica) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Contract source does not exist: $Source" }
    if (-not (Test-Path -LiteralPath $Replica -PathType Leaf)) { throw "Contract replica does not exist: $Replica" }
    $sourceBytes = [IO.File]::ReadAllBytes($Source)
    $replicaBytes = [IO.File]::ReadAllBytes($Replica)
    if ($sourceBytes.Length -ne $replicaBytes.Length) { throw "Contract fixture is out of sync: $Replica" }
    for ($index = 0; $index -lt $sourceBytes.Length; $index++) {
        if ($sourceBytes[$index] -ne $replicaBytes[$index]) { throw "Contract fixture is out of sync: $Replica" }
    }
    Write-Host "OK  $Replica"
}

$contextRevision = (& git -C $ContextPath rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $contextRevision -cnotmatch '^[0-9a-f]{40}$') {
    throw "Unable to resolve Context commit at $ContextPath"
}
$revisionSnapshots = @(
    (Join-Path $CoreApiPath 'openapi/CONTEXT_REVISION'),
    (Join-Path $ComputeApiPath 'openapi/CONTEXT_REVISION')
)
if ($IncludeWeb) { $revisionSnapshots += (Join-Path $WebPath 'docs/api/CONTEXT_REVISION') }
foreach ($revisionSnapshot in $revisionSnapshots) {
    if (-not (Test-Path -LiteralPath $revisionSnapshot -PathType Leaf)) {
        throw "Context revision snapshot does not exist: $revisionSnapshot"
    }
    $revision = [IO.File]::ReadAllText($revisionSnapshot).Trim()
    if ($revision -cne $contextRevision) {
        throw "Context revision is out of sync: $revisionSnapshot (expected $contextRevision)"
    }
    Write-Host "OK  $revisionSnapshot"
}

$fixturePath = Join-Path $ContextPath 'fixtures/trading-normalization'
$coreFixturePath = Join-Path $CoreApiPath 'tradingreview/application/src/test/resources/trading-normalization'
$computeFixturePath = Join-Path $ComputeApiPath 'tests/fixtures/trading_review/normalization_identity/v1'
foreach ($name in @('README.md', 'input-identity-v1.json', 'input-identity-v1.schema.json', 'wire-v1.json')) {
    $source = Join-Path $fixturePath $name
    Assert-SameBytes $source (Join-Path $coreFixturePath $name)
    Assert-SameBytes $source (Join-Path $computeFixturePath $name)
}

$vector = Get-Content -LiteralPath (Join-Path $fixturePath 'input-identity-v1.json') -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($vector.specificationVersion) -or
    $vector.specificationVersion -cne $vector.inputIdentity.hashSpecificationVersion) {
    throw 'Normalization identity specification version is inconsistent.'
}
$canonicalBytes = [Text.Encoding]::UTF8.GetBytes($vector.canonicalJson)
$canonicalHex = [BitConverter]::ToString($canonicalBytes).Replace('-', '').ToLowerInvariant()
if ($canonicalHex -cne $vector.canonicalUtf8Hex) {
    throw 'Normalization identity canonical UTF-8 bytes do not match the Context vector.'
}
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $calculatedHash = [BitConverter]::ToString($sha256.ComputeHash($canonicalBytes)).Replace('-', '').ToLowerInvariant()
}
finally { $sha256.Dispose() }
if ($calculatedHash -cne $vector.expectedSha256) {
    throw 'Normalization identity SHA-256 does not match the Context vector.'
}
Write-Host "Normalization identity vector synchronized: $($vector.specificationVersion) $calculatedHash"
