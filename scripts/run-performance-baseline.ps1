[CmdletBinding()]
param(
    [ValidateSet('Smoke', 'Official')][string]$Mode = 'Smoke',
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$ContextPath = (Join-Path $PSScriptRoot '..\..\context'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web'),
    [string]$Schema,
    [string]$SnapshotStorageRoot = '.integration-data\performance-snapshots',
    [string]$OutputRoot = '.integration-data\performance\baselines',
    [Nullable[int]]$Warmups,
    [Nullable[int]]$Iterations,
    [Nullable[int]]$Batches,
    [ValidateSet('warm', 'process-cold')][string]$CacheMode = 'warm'
)

$ErrorActionPreference = 'Stop'
function Write-BaselineJson([object]$Value, [string]$Path) {
    $json = $Value | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($Path, "$json`n", [Text.UTF8Encoding]::new($false))
}
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$compute = [IO.Path]::GetFullPath($ComputeApiPath)
$context = [IO.Path]::GetFullPath($ContextPath)
$core = [IO.Path]::GetFullPath($CoreApiPath)
$web = [IO.Path]::GetFullPath($WebPath)
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
if ($values['POSTGRES_DB'] -cne 'refinvest_integration') { throw 'Baseline work is allowed only with refinvest_integration.' }
if ($CacheMode -ne 'warm') { throw 'Compute does not implement process-cold; it cannot be emulated.' }

$protocolPath = Join-Path $workspace 'performance\baseline-protocol.json'
$expectedPath = Join-Path $workspace 'performance\expected-results.json'
$manifestPath = Join-Path $workspace 'performance\fixture-manifest.json'
$scenarioDirectory = Join-Path $workspace 'performance\scenarios'
$protocol = Get-Content -Raw -LiteralPath $protocolPath | ConvertFrom-Json
$effectiveWarmups = if ($null -ne $Warmups) { $Warmups } elseif ($Mode -eq 'Official') { $protocol.warmupsPerBatch } else { 0 }
$effectiveIterations = if ($null -ne $Iterations) { $Iterations } elseif ($Mode -eq 'Official') { $protocol.iterationsPerBatch } else { 1 }
$effectiveBatches = if ($null -ne $Batches) { $Batches } elseif ($Mode -eq 'Official') { $protocol.batches } else { 1 }
if ($effectiveWarmups -lt 0 -or $effectiveIterations -lt 1 -or $effectiveBatches -lt 1) { throw 'Invalid execution counts.' }
$official = $Mode -eq 'Official' -and $effectiveWarmups -eq $protocol.warmupsPerBatch -and $effectiveIterations -eq $protocol.iterationsPerBatch -and $effectiveBatches -eq $protocol.batches -and $CacheMode -eq $protocol.cacheModes[0]
if ($Mode -eq 'Official' -and -not $official) { throw 'Official mode cannot use protocol overrides.' }

& (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') -ContextPath $context -CoreApiPath $core -ComputeApiPath $compute
if (-not $?) { throw 'Contract sync verification failed.' }
$storageRoot = Resolve-IntegrationWorkspacePath $workspace $SnapshotStorageRoot
$snapshotPath = Join-Path $storageRoot 'performance-mvp-daily-v1'
if (-not (Test-Path -LiteralPath $snapshotPath)) {
    & (Join-Path $PSScriptRoot 'publish-performance-fixture.ps1') -EnvironmentFile $EnvironmentFile -ComputeApiPath $compute -Schema $Schema -SnapshotStorageRoot $SnapshotStorageRoot
    if (-not $?) { throw 'Performance fixture publication failed.' }
}
$python = Get-IntegrationPython -ComputeApiPath $compute
& $python (Join-Path $PSScriptRoot 'verify-performance-fixture.py') --definition (Join-Path $workspace 'performance\fixture-definition.json') --fixture-manifest $manifestPath --scenario-directory $scenarioDirectory --snapshot-path $snapshotPath --compute-api-path $compute
if ($LASTEXITCODE -ne 0) { throw 'Performance fixture verification failed.' }

$outputBase = Resolve-IntegrationWorkspacePath $workspace $OutputRoot
$shortComputeRevision = (& git -C $compute rev-parse --short=8 HEAD).Trim()
$baselineId = "$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))-compute-$shortComputeRevision"
$runDirectory = Join-Path $outputBase $baselineId
if (Test-Path -LiteralPath $runDirectory) { throw "Baseline output already exists: $runDirectory" }
$reportsDirectory = Join-Path $runDirectory 'reports'
New-Item -ItemType Directory -Path $reportsDirectory | Out-Null

$packageVersion = (& $python -c "from importlib.metadata import version; print(version('refinvest'))").Trim()
$environment = [ordered]@{
    os = [System.Environment]::OSVersion.VersionString
    platform = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
    machine = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    cpu = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Name)
    logicalCpuCount = [System.Environment]::ProcessorCount
    memoryBytes = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TotalPhysicalMemory)
    pythonVersion = (& $python --version 2>&1).ToString().Replace('Python ', '')
    engineVersion = $packageVersion
    computePackageVersion = $packageVersion
    dependencyLockSha256 = 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $compute 'uv.lock')).Hash.ToLowerInvariant()
}
Write-BaselineJson $environment (Join-Path $runDirectory 'environment.json')
$revisions = [ordered]@{ integration = (& git -C $workspace rev-parse HEAD).Trim(); context = (& git -C $context rev-parse HEAD).Trim(); compute = (& git -C $compute rev-parse HEAD).Trim(); core = if (Test-Path (Join-Path $core '.git')) { (& git -C $core rev-parse HEAD).Trim() } else { $null }; web = if (Test-Path (Join-Path $web '.git')) { (& git -C $web rev-parse HEAD).Trim() } else { $null } }
Write-BaselineJson $revisions (Join-Path $runDirectory 'revisions.json')

$scenarioFiles = Get-ChildItem -LiteralPath $scenarioDirectory -Filter '*.json' | Sort-Object BaseName
$invocationFailures = @()
Push-Location $compute
try {
    foreach ($scenario in $scenarioFiles) {
        foreach ($batch in 1..$effectiveBatches) {
            $report = Join-Path $reportsDirectory "$($scenario.BaseName)-batch-$batch.json"
            $arguments = @('-m', 'refinvest.app.benchmark', '--fixture-manifest', $manifestPath, '--snapshot-path', $snapshotPath, '--scenario', $scenario.FullName, '--warmups', $effectiveWarmups, '--iterations', $effectiveIterations, '--cache-mode', $CacheMode, '--output', $report, '--expected-results', $expectedPath)
            & $python @arguments
            if ($LASTEXITCODE -ne 0) {
                $invocationFailures += [ordered]@{ scenarioId = $scenario.BaseName; batch = $batch; exitCode = $LASTEXITCODE; reportWritten = (Test-Path -LiteralPath $report) }
            }
        }
    }
} finally { Pop-Location }
$failureDocument = [ordered]@{ failures = @($invocationFailures) }
Write-BaselineJson $failureDocument (Join-Path $runDirectory 'invocation-failures.json')

$builderArguments = @((Join-Path $PSScriptRoot 'build-performance-baseline-report.py'), '--protocol', $protocolPath, '--expected-results', $expectedPath, '--fixture-manifest', $manifestPath, '--scenario-directory', $scenarioDirectory, '--reports-directory', $reportsDirectory, '--environment', (Join-Path $runDirectory 'environment.json'), '--revisions', (Join-Path $runDirectory 'revisions.json'), '--output-json', (Join-Path $runDirectory 'baseline.json'), '--output-markdown', (Join-Path $runDirectory 'baseline.md'), '--baseline-id', $baselineId, '--cache-mode', $CacheMode, '--warmups', $effectiveWarmups, '--iterations', $effectiveIterations, '--batches', $effectiveBatches)
if ($official) { $builderArguments += '--official' }
& $python @builderArguments
$builderExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath (Join-Path $runDirectory 'baseline.json')) {
    & $python (Join-Path $PSScriptRoot 'verify-performance-baseline.py') --baseline (Join-Path $runDirectory 'baseline.json') --protocol $protocolPath --expected-results $expectedPath --fixture-manifest $manifestPath --scenario-directory $scenarioDirectory
    $verificationExitCode = $LASTEXITCODE
} else {
    $verificationExitCode = 1
}
Write-Output "Baseline JSON: $(Join-Path $runDirectory 'baseline.json')"
Write-Output "Baseline Markdown: $(Join-Path $runDirectory 'baseline.md')"
if ($invocationFailures.Count -gt 0 -or $builderExitCode -ne 0 -or $verificationExitCode -ne 0) {
    throw "Baseline failed; diagnostics were preserved in $runDirectory"
}
