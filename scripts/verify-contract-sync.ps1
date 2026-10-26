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

# Core owns document snapshots, including the result delivery policy and
# acceptance/failure rules that are not represented in OpenAPI.
$coreDocVerifier = Join-Path $CoreApiPath 'scripts/verify-context-doc-sync.ps1'
if (-not (Test-Path -LiteralPath $coreDocVerifier -PathType Leaf)) {
    throw "Core document snapshot verifier does not exist: $coreDocVerifier"
}
& $coreDocVerifier -ContextPath $ContextPath

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

$revisionArguments = @(
    '-B', (Join-Path $PSScriptRoot 'contract_sync.py'), 'verify-revisions',
    '--context', $ContextPath, '--core-api', $CoreApiPath,
    '--compute-api', $ComputeApiPath, '--integration', (Join-Path $PSScriptRoot '..')
)
if ($IncludeWeb) { $revisionArguments += @('--include-web', '--web', $WebPath) }
& python @revisionArguments
if ($LASTEXITCODE -ne 0) { throw 'Context revision provenance verification failed.' }

$fixturePath = Join-Path $ContextPath 'fixtures/trading-normalization'
$coreFixturePath = Join-Path $CoreApiPath 'tradingreview/application/src/test/resources/trading-normalization'
$computeFixturePath = Join-Path $ComputeApiPath 'tests/fixtures/trading_review/normalization_identity/v1'
foreach ($name in @('README.md', 'input-identity-v1.json', 'input-identity-v1.schema.json',
                    'result-integrity-v1.json', 'result-integrity-v1.schema.json', 'wire-v1.json')) {
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

# Hash the Context oracle's stored canonical bytes directly. This is an independent
# fixture check, not an implementation of the production canonicalization rules.
$resultVector = Get-Content -LiteralPath (Join-Path $fixturePath 'result-integrity-v1.json') -Raw | ConvertFrom-Json
if ($resultVector.specificationVersion -cne $resultVector.terminalPayload.resultHashSpecificationVersion -or
    $resultVector.resultSchemaVersion -cne $resultVector.terminalPayload.resultSchemaVersion) {
    throw 'Normalization result vector versions are inconsistent.'
}
foreach ($entry in @($resultVector.sectionVectors.PSObject.Properties | ForEach-Object { $_.Value }) +
                    @($resultVector.manifestVector, $resultVector.resultDigestManifestVector)) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($entry.canonicalJson)
    $hex = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    if ($hex -cne $entry.canonicalUtf8Hex) {
        throw 'Normalization result canonical UTF-8 bytes do not match the Context vector.'
    }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    if ($digest -cne $entry.expectedSha256) {
        throw 'Normalization result SHA-256 does not match the Context vector.'
    }
}
$payload = $resultVector.terminalPayload
if ($payload.reconciliationManifest.manifestHash -cne $resultVector.manifestVector.expectedSha256 -or
    $payload.resultDigestManifest.reconciliationManifestHash -cne $resultVector.manifestVector.expectedSha256 -or
    $payload.resultHash -cne $resultVector.resultDigestManifestVector.expectedSha256) {
    throw 'Normalization result manifest or overall hash is inconsistent.'
}
foreach ($sectionDigest in $payload.sectionDigests) {
    $section = $resultVector.sectionVectors.PSObject.Properties[$sectionDigest.sectionType]
    if ($null -eq $section -or $sectionDigest.sectionHash -cne $section.Value.expectedSha256) {
        throw "Normalization result section hash is inconsistent: $($sectionDigest.sectionType)"
    }
}
Write-Host "Normalization result vector synchronized: $($resultVector.specificationVersion) $($payload.resultHash)"

# The V1 oracle remains pinned. The V2 oracle adds one singleton section and
# uses a new schema/hash pair; both versions stay independently verifiable.
$v1Sections = @('ARTIFACT_COVERAGES', 'CANONICAL_RECORDS', 'CAPABILITIES',
                'QUALITY_REPORT', 'RECONCILIATION_ISSUES', 'RECONCILIATION_MANIFEST',
                'RECONCILIATION_MATCHES', 'REVIEW_UNITS', 'SOURCE_EVIDENCE_SNAPSHOTS') | Sort-Object -CaseSensitive
if ($resultVector.specificationVersion -cne 'TRADING_NORMALIZATION_RESULT_HASH_V1' -or
    $resultVector.resultSchemaVersion -cne 'TRADING_NORMALIZATION_RESULT_V1' -or
    $null -ne $payload.analyticsInputSupplement -or
    @($payload.sectionDigests).Count -ne 9 -or
    (@($payload.sectionDigests | ForEach-Object { $_.sectionType }) -join ',') -cne ($v1Sections -join ',')) {
    throw 'Normalization V1 result contract has changed.'
}

$v2Name = 'result-integrity-v2.json'
$v2Source = Join-Path $fixturePath $v2Name
$v2Replicas = @(
    (Join-Path $coreFixturePath $v2Name),
    (Join-Path $ComputeApiPath 'tests/fixtures/trading_review/normalization_identity/v2/result-integrity-v2.json'),
    (Join-Path $PSScriptRoot '../fixtures/trading-review/normalization-result-v2/result-integrity-v2.json')
)
foreach ($replica in $v2Replicas) { Assert-SameBytes $v2Source $replica }
$v2 = Get-Content -LiteralPath $v2Replicas[2] -Raw | ConvertFrom-Json
$v2Payload = $v2.terminalPayload
$v2Spec = 'TRADING_NORMALIZATION_RESULT_HASH_V2'
$v2Schema = 'TRADING_NORMALIZATION_RESULT_V2'
if ($v2.specificationVersion -cne $v2Spec -or $v2.resultSchemaVersion -cne $v2Schema -or
    $v2Payload.resultHashSpecificationVersion -cne $v2Spec -or
    $v2Payload.resultSchemaVersion -cne $v2Schema -or
    $v2Payload.resultDigestManifest.resultHashSpecificationVersion -cne $v2Spec -or
    $v2Payload.resultDigestManifest.resultSchemaVersion -cne $v2Schema -or
    $v2Payload.reconciliationManifest.manifestHashSpecificationVersion -cne 'TRADING_NORMALIZATION_RESULT_HASH_V1' -or
    $v2Payload.analyticsInputSupplement.schemaVersion -cne 'TRADING_ANALYTICS_INPUT_SUPPLEMENT_V1') {
    throw 'Normalization V2 result schema/hash version pair is inconsistent.'
}
$v2Sections = @('ANALYTICS_INPUT_SUPPLEMENT') + $v1Sections | Sort-Object -CaseSensitive
if (@($v2Payload.sectionDigests).Count -ne 10 -or
    @($v2.sectionVectors.PSObject.Properties).Count -ne 10 -or
    (@($v2Payload.sectionDigests | ForEach-Object { $_.sectionType }) -join ',') -cne ($v2Sections -join ',') -or
    ($v2Payload.resultDigestManifest.sectionDigests | ConvertTo-Json -Depth 20 -Compress) -cne
    ($v2Payload.sectionDigests | ConvertTo-Json -Depth 20 -Compress)) {
    throw 'Normalization V2 section composition or order is inconsistent.'
}
foreach ($digest in $v2Payload.sectionDigests) {
    $entry = $v2.sectionVectors.PSObject.Properties[$digest.sectionType]
    if ($null -eq $entry -or $digest.hashSpecificationVersion -cne $v2Spec -or
        $digest.sectionHash -cne $entry.Value.expectedSha256) {
        throw "Normalization V2 section digest is inconsistent: $($digest.sectionType)"
    }
}
foreach ($entry in @($v2.sectionVectors.PSObject.Properties | ForEach-Object { $_.Value }) +
                    @($v2.manifestVector, $v2.resultDigestManifestVector)) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($entry.canonicalJson)
    $hex = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    if ($hex -cne $entry.canonicalUtf8Hex -or $digest -cne $entry.expectedSha256) {
        throw 'Normalization V2 fixed vector bytes or SHA-256 are inconsistent.'
    }
}
$manifestDigest = $v2Payload.sectionDigests | Where-Object { $_.sectionType -ceq 'RECONCILIATION_MANIFEST' }
if ($v2Payload.reconciliationManifest.manifestHash -cne $v2.manifestVector.expectedSha256 -or
    $manifestDigest.sectionHash -cne $v2.manifestVector.expectedSha256 -or
    $v2Payload.resultDigestManifest.reconciliationManifestHash -cne $v2.manifestVector.expectedSha256 -or
    $v2Payload.resultHash -cne $v2.resultDigestManifestVector.expectedSha256) {
    throw 'Normalization V2 manifest or result hash is inconsistent.'
}
foreach ($name in @('verify-trading-normalization-result-integrity.py',
                     'verify-trading-normalization-result-v2.py')) {
    $verifier = Join-Path $ContextPath "scripts/$name"
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) { throw "Missing Context verifier: $verifier" }
    & python -B $verifier
    if ($LASTEXITCODE -ne 0) { throw "Normalization schema/vector verification failed: $name" }
}
Write-Host "Normalization V1/V2 schema and vector contracts verified: $($v2Payload.resultHash)"
# Analytics identity and result vectors are Context-owned protocol fixtures.
# Keep byte-for-byte snapshots in each runtime repository and this harness so
# the Core/Compute implementations can consume the same independent oracle.
$analyticsFixturePath = Join-Path $ContextPath 'fixtures/trading-analytics'
$coreAnalyticsFixturePath = Join-Path $CoreApiPath 'tradingreview/application/src/test/resources/trading-analytics-protocol/v1'
$computeAnalyticsFixturePath = Join-Path $ComputeApiPath 'tests/fixtures/trading_review/analytics_protocol/v1'
$integrationAnalyticsFixturePath = Join-Path $PSScriptRoot '..\fixtures\trading-review\analytics-protocol-v1'
foreach ($name in @('README.md', 'input-identity-v1.json', 'input-identity-v1.schema.json',
                    'result-integrity-v1.json', 'result-integrity-v1.schema.json')) {
    $source = Join-Path $analyticsFixturePath $name
    Assert-SameBytes $source (Join-Path $coreAnalyticsFixturePath $name)
    Assert-SameBytes $source (Join-Path $computeAnalyticsFixturePath $name)
    Assert-SameBytes $source (Join-Path $integrationAnalyticsFixturePath $name)
}

Write-Host 'Trading Analytics V1 vectors synchronized with Context.'

# Context owns the Analytics create response schemas and their fixed-vector checks.
# The OpenAPI snapshots above must match before this normative verifier runs.
$analyticsContractVerifier = Join-Path $ContextPath 'scripts/verify-trading-analytics-contract.py'
if (-not (Test-Path -LiteralPath $analyticsContractVerifier -PathType Leaf)) {
    throw "Context Analytics contract verifier does not exist: $analyticsContractVerifier"
}
& python $analyticsContractVerifier
if ($LASTEXITCODE -ne 0) {
    throw 'Context Analytics create/replay contract verification failed.'
}
Write-Host 'Trading Analytics create 202, replay 200, and conflict 409 contract verified.'
