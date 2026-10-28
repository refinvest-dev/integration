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
    [int]$TimeoutSeconds = 180,
    [switch]$ResultIntegrity,
    [switch]$Publication,
    [switch]$ResultVersionPin
)
$ErrorActionPreference = 'Stop'
if ($ResultVersionPin) { $Publication = $true }
if (-not $TestRoot) { $TestRoot = Join-Path $PSScriptRoot '..' }
if (-not $ContextPath) { $ContextPath = Join-Path $TestRoot '..\context' }
if (-not $CorePath) { $CorePath = Join-Path $TestRoot '..\api\refinvest' }
if (-not $ComputePath) { $ComputePath = Join-Path $TestRoot '..\compute-api\refinvest' }
if (-not $WebPath) { $WebPath = Join-Path $TestRoot '..\web\web' }
if (-not $EnvironmentFile) { $EnvironmentFile = Join-Path $TestRoot '.env.local' }
$TestRoot = [IO.Path]::GetFullPath($TestRoot)

# This must be the first executable test gate. No process or DB mutation precedes it.
& (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') -ContextPath $ContextPath `
    -CoreApiPath $CorePath -ComputeApiPath $ComputePath -WebPath $WebPath -IncludeWeb
if ($LASTEXITCODE -ne 0) { throw 'Contract preflight failed.' }

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Assert-IntegrationEnvironment $values @('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
    'INTEGRATION_POSTGRES_PORT', 'REFINVEST_INTERNAL_API_KEY')
if ($DatabaseName -cne 'refinvest_integration' -or $values.POSTGRES_DB -cne $DatabaseName) {
    throw 'Runtime contract tests require the dedicated refinvest_integration database.'
}
$dbHost = Get-IntegrationPostgresHost $values
if ($dbHost -notin @('localhost', '127.0.0.1', '::1')) {
    throw 'Runtime contract tests require local PostgreSQL.'
}
if ($CorePort -eq 0) { $CorePort = [int]$values.INTEGRATION_CORE_PORT }
if ($ComputePort -eq 0) { $ComputePort = [int]$values.INTEGRATION_COMPUTE_PORT }
if ($CorePort -notin 1..65535 -or $ComputePort -notin 1..65535 -or $CorePort -eq $ComputePort) {
    throw 'Core and Compute require distinct valid ports.'
}
$fixturePort = $ComputePort + 1
if ($fixturePort -gt 65535 -or $fixturePort -eq $CorePort) { throw 'Fixture port conflicts with a service port.' }
$psql = Get-IntegrationPsql
if (-not $psql) { throw 'Local psql is required for the runtime database identity check.' }
$env:PGPASSWORD = $values.POSTGRES_PASSWORD
$dbTarget = (& $psql -h $dbHost -p $values.INTEGRATION_POSTGRES_PORT -U $values.POSTGRES_USER `
    -d $DatabaseName -tA -v ON_ERROR_STOP=1 -c 'SELECT current_database();').Trim()
if ($LASTEXITCODE -ne 0 -or $dbTarget -cne $DatabaseName) { throw 'Resolved PostgreSQL database is not the integration database.' }
Write-Host "Database target verified: $dbHost`:$($values.INTEGRATION_POSTGRES_PORT)/$DatabaseName"

$runId = [guid]::NewGuid().ToString('N')
$registry = "trading-normalization-$($runId.Substring(0,12))"
$computeSchema = "tnorm_$($runId.Substring(0,12))"
$coreSchema = if ($ResultVersionPin) { "tnorm_core$($runId.Substring(0,12))" } else { 'public' }
$runDir = Join-Path $TestRoot ".integration-data\trading-normalization\$runId"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$activeEnvironmentFile = $EnvironmentFile
if ($CorePort -ne [int]$values.INTEGRATION_CORE_PORT -or
    $ComputePort -ne [int]$values.INTEGRATION_COMPUTE_PORT) {
    $activeEnvironmentFile = Join-Path $runDir 'runtime.env'
    $overrides = @{'INTEGRATION_CORE_PORT' = [string]$CorePort;
                   'INTEGRATION_COMPUTE_PORT' = [string]$ComputePort}
    $lines = foreach ($line in Get-Content -LiteralPath $EnvironmentFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=') {
            $name = $Matches[1]
            if ($overrides.ContainsKey($name)) { "$name=$($overrides[$name])"; continue }
        }
        $line
    }
    [IO.File]::WriteAllLines($activeEnvironmentFile, [string[]]$lines, [Text.UTF8Encoding]::new($false))
}
$started = $false
$scenario = 'configuration'
try {
    Write-Host 'Preparing synthetic fixture and runtime configuration.'
    $python = Get-IntegrationPython -ComputeApiPath $ComputePath
    $fixtureRoot = Join-Path $TestRoot 'fixtures\trading-review\binance-usds-v1-raw-v3\common-window-exact-reconciliation'
    $manifest = Get-Content -LiteralPath (Join-Path $fixtureRoot 'fixture-manifest.json') -Raw | ConvertFrom-Json
    $instrumentId = [string]$manifest.instrumentDescriptors[0].instrumentId
    $symbol = [string]$manifest.instrumentDescriptors[0].sourceSymbol
    $termsVersion = 'INTEGRATION_SYNTHETIC_TERMS_V1'
    $versionJson = (& $python (Join-Path $PSScriptRoot 'trading_normalization_runtime_config.py') `
        $ComputePath $instrumentId $termsVersion | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionJson)) {
        throw 'Compute executable VersionSet registry could not provide a test pin.'
    }
    Write-Host 'Compute VersionSet pin prepared.'
    $null = $versionJson | ConvertFrom-Json
    $terms = ConvertTo-Json -InputObject @(@{
        instrumentId = $instrumentId; venueSymbol = $symbol; settlementAsset = 'USDT'
        quantityStep = '0.001'; priceTick = '0.01'; contractMultiplier = '1'
        settlementPrecision = 8; instrumentTermsVersion = $termsVersion
    }) -Depth 5 -Compress

    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $secretBytes = New-Object byte[] 36
        $random.GetBytes($secretBytes)
        $secret = [Convert]::ToBase64String($secretBytes)
        $keyBytes = New-Object byte[] 32
        $random.GetBytes($keyBytes)
        $payloadKey = [Convert]::ToBase64String($keyBytes)
    } finally { $random.Dispose() }
    $keystore = Join-Path $runDir 'raw-wrapping.p12'
    Write-Host 'Preparing local encrypted Artifact storage.'
    # keytool prints successful generation to stderr; PowerShell 5.1 otherwise
    # promotes that line to a terminating error under Stop.
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & keytool -genseckey -alias integration-v1 -keyalg AES -keysize 256 -storetype PKCS12 `
            -keystore $keystore -storepass $secret -keypass $secret 2>&1 | Out-Null
    } finally { $ErrorActionPreference = $previousErrorAction }
    if ($LASTEXITCODE -ne 0) { throw 'Local Artifact wrapping keystore creation failed.' }
    Write-Host 'Local encrypted Artifact storage ready.'
    $rawRoot = Join-Path $runDir 'encrypted-artifacts'
    New-Item -ItemType Directory -Force -Path $rawRoot | Out-Null
    $springConfig = @{
        'refinvest.trading-review.raw.root' = $rawRoot
        'refinvest.trading-review.raw.keystore-path' = $keystore
        'refinvest.trading-review.raw.keystore-password' = $secret
        'refinvest.trading-review.raw.key-alias' = 'integration-v1'
        'refinvest.trading-review.version-set-json' = $versionJson
        'refinvest.trading-review.normalization.instrument-terms-json' = $terms
        'refinvest.trading-review.grant-base-url' = "http://127.0.0.1:$CorePort"
    }
    if ($Publication) {
        $springConfig['refinvest.compute.ack-delay'] = 'PT20S'
        $springConfig['refinvest.compute.result-delay'] = 'PT1M'
    }
    $env:SPRING_APPLICATION_JSON = $springConfig | ConvertTo-Json -Depth 6 -Compress
    $env:REFINVEST_INTERNAL_API_KEY = $values.REFINVEST_INTERNAL_API_KEY
    $env:REFINVEST_TRADING_REVIEW_TZDB_VERSION = 'integration-pinned-tzdb-v1'
    $env:REFINVEST_TRADING_IDENTITY_KEY = $secret
    $env:REFINVEST_TRADING_MASKING_KEY = $secret
    $env:REFINVEST_TRADING_REVIEW_PAYLOAD_KEY = $payloadKey
    $env:REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS = "127.0.0.1:$CorePort,127.0.0.1:$fixturePort"
    $env:REFINVEST_TRADING_REVIEW_ACTIVE_JOB_LIMIT = '20'

    $scenario = 'migration'
    $coreMigrationRoot = Join-Path $CorePath 'tradingreview\adapter\persistence\src\main\resources\db\migration'
    $expectedCoreMigrations = @(Get-ChildItem -LiteralPath $coreMigrationRoot `
        -Filter 'V*__*.sql' | Select-Object -ExpandProperty Name | Sort-Object)
    $appliedCoreMigrations = @()
    for ($inspection = 0; $inspection -lt 3; $inspection++) {
        $previousErrorAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $appliedCoreMigrations = @(& $psql -h $dbHost -p $values.INTEGRATION_POSTGRES_PORT `
                -U $values.POSTGRES_USER -d $DatabaseName -tA -v ON_ERROR_STOP=1 `
                -c "SELECT filename FROM $coreSchema.integration_core_trading_migrations ORDER BY filename;" 2>$null)
        } finally { $ErrorActionPreference = $previousErrorAction }
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($LASTEXITCODE -ne 0 -or
        (($expectedCoreMigrations -join ',') -cne (($appliedCoreMigrations | Sort-Object) -join ','))) {
        & (Join-Path $PSScriptRoot 'initialize-core-trading-schema.ps1') -EnvironmentFile $activeEnvironmentFile `
            -CorePath $CorePath -DatabaseSchema $coreSchema
        if ($LASTEXITCODE -ne 0) { throw 'Core trading migration failed.' }
    } else { Write-Host 'Core trading migrations are already applied in the integration database.' }
    if ($ResultVersionPin) {
        Set-ComputeIntegrationEnvironment $values $TestRoot $computeSchema
        & $python -B (Join-Path $PSScriptRoot 'initialize-trading-normalization-compute.py') `
            --compute-path $ComputePath --schema $computeSchema
    } else {
        & (Join-Path $PSScriptRoot 'initialize-compute-schema.ps1') -EnvironmentFile $activeEnvironmentFile `
            -ComposeFile (Join-Path $TestRoot 'compose.yaml') -ComputeApiPath $ComputePath -Schema $computeSchema
    }
    if ($LASTEXITCODE -ne 0) { throw 'Compute migration failed.' }
    Remove-Item Env:PGOPTIONS -ErrorAction SilentlyContinue
    if ($ResultVersionPin) {
        $env:GRADLE_USER_HOME = Join-Path $TestRoot '.integration-data\gradle'
        $buildLog = Join-Path $runDir 'core-bootjar.log'
        $previousDirectory = Get-Location
        $previousErrorAction = $ErrorActionPreference
        try {
            Set-Location -LiteralPath $CorePath
            $ErrorActionPreference = 'Continue'
            & .\gradlew.bat :app:bootJar --console=plain --no-daemon *> $buildLog
            $jarBuildExit = $LASTEXITCODE
        } finally { Set-Location -LiteralPath $previousDirectory; $ErrorActionPreference = $previousErrorAction }
        if ($jarBuildExit -ne 0) { throw 'Core executable JAR build failed; see core-bootjar.log.' }
        $jars = @(Get-ChildItem -LiteralPath (Join-Path $CorePath 'app/build/libs') -Filter '*.jar' |
            Where-Object { $_.Name -notmatch '-plain\.jar$' })
        if ($jars.Count -ne 1) { throw 'Expected one Core executable JAR.' }
        $env:TRADING_RUNTIME_CORE_JAR = $jars[0].FullName
    }
    $scenario = 'startup'
    $started = $true
    if ($ResultVersionPin) {
        & (Join-Path $PSScriptRoot 'start-trading-normalization-service.ps1') -EnvironmentFile $activeEnvironmentFile `
            -Service compute-api -ComputePath $ComputePath -DatabaseSchema $computeSchema `
            -RegistryId $registry -EvidenceDirectory $runDir
        & (Join-Path $PSScriptRoot 'start-trading-normalization-service.ps1') -EnvironmentFile $activeEnvironmentFile `
            -CorePath $CorePath -RegistryId $registry -ComputeBaseUrl "http://127.0.0.1:$fixturePort" `
            -EvidenceDirectory $runDir -DatabaseSchema $coreSchema
    } else {
        & (Join-Path $PSScriptRoot 'start-compute-api.ps1') -EnvironmentFile $activeEnvironmentFile `
            -ComputeApiPath $ComputePath -Schema $computeSchema -Profile $registry -RegistryId $registry
        & (Join-Path $PSScriptRoot 'start-core-api.ps1') -EnvironmentFile $activeEnvironmentFile `
            -CoreApiPath $CorePath -Profile $registry -RegistryId $registry
    }
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://127.0.0.1:$ComputePort/health" -TimeoutSeconds 90
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://127.0.0.1:$CorePort/actuator/health" -TimeoutSeconds 600
    # Start the worker only after API health and a fresh DB connection. On Windows,
    # a transient socket failure can terminate the worker before the first claim.
    $worker = & (Join-Path $PSScriptRoot 'start-compute-worker.ps1') -EnvironmentFile $activeEnvironmentFile `
        -ComputeApiPath $ComputePath -Schema $computeSchema -Profile $registry -RegistryId $registry -TradingReview
    $workerAlive = $false
    for ($probe = 0; $probe -lt 12; $probe++) {
        Start-Sleep -Milliseconds 250
        $workerAlive = $null -ne (Get-Process -Id $worker.processId -ErrorAction SilentlyContinue)
        if (-not $workerAlive) { break }
    }
    if (-not $workerAlive) {
        & (Join-Path $PSScriptRoot 'stop-integration.ps1') -RegistryId $registry -Service compute-worker | Out-Null
        $worker = & (Join-Path $PSScriptRoot 'start-compute-worker.ps1') -EnvironmentFile $activeEnvironmentFile `
            -ComputeApiPath $ComputePath -Schema $computeSchema -Profile $registry -RegistryId $registry -TradingReview
        $workerAlive = $false
        for ($probe = 0; $probe -lt 12; $probe++) {
            Start-Sleep -Milliseconds 250
            $workerAlive = $null -ne (Get-Process -Id $worker.processId -ErrorAction SilentlyContinue)
            if (-not $workerAlive) { break }
        }
        if (-not $workerAlive) {
            throw 'Compute worker exited during bounded startup verification.'
        }
    }
    $scenario = 'runtime'
    $token = & (Join-Path $PSScriptRoot 'seed-core-member.ps1') -EnvironmentFile $activeEnvironmentFile -Tier PRO -DatabaseSchema $coreSchema |
        Select-Object -Last 1
    if (-not $token) { throw 'Integration member token could not be minted.' }
    $env:TRADING_RUNTIME_ACCESS_TOKEN = $token
    $env:TRADING_RUNTIME_DB_HOST = $dbHost
    $env:TRADING_RUNTIME_DB_PORT = $values.INTEGRATION_POSTGRES_PORT
    $env:TRADING_RUNTIME_DB_NAME = $DatabaseName
    $env:TRADING_RUNTIME_DB_USER = $values.POSTGRES_USER
    $env:TRADING_RUNTIME_DB_PASSWORD = $values.POSTGRES_PASSWORD
    $env:TRADING_RUNTIME_PSQL = $psql
    $env:TRADING_RUNTIME_COMPUTE_SCHEMA = $computeSchema
    $env:TRADING_RUNTIME_CORE_SCHEMA = $coreSchema
    $env:TRADING_RUNTIME_API_KEY = $values.REFINVEST_INTERNAL_API_KEY
    $env:TRADING_RUNTIME_VERSION_SET = $versionJson
    $runtimeArguments = @()
    if ($ResultIntegrity) { $runtimeArguments += '--result-integrity' }
    if ($Publication) { $runtimeArguments += '--publication' }
    if ($ResultVersionPin) { $runtimeArguments += '--result-version-pin' }
    & $python (Join-Path $PSScriptRoot 'test_trading_normalization_runtime_contract.py') `
        --core-url "http://127.0.0.1:$CorePort" --compute-url "http://127.0.0.1:$ComputePort" `
        --fixture-root $fixtureRoot --context-path $ContextPath --run-dir $runDir `
        --fixture-port $fixturePort --test-root $TestRoot --environment-file $activeEnvironmentFile `
        --core-path $CorePath --compute-path $ComputePath --web-path $WebPath --compute-schema $computeSchema --registry $registry `
        --timeout $TimeoutSeconds @runtimeArguments
    if ($LASTEXITCODE -ne 0) { throw 'Runtime contract scenario failed. See evidence and logs.' }
    if ($ResultVersionPin) {
        Write-Host 'V1 result pin and ORCH-024~028 runtime assertions PASS.'
    }
    Write-Host "Trading Normalization runtime contract PASS. Evidence: $runDir"
} catch {
    [Console]::Error.WriteLine("Scenario $scenario failed: $($_.Exception.Message)")
    Write-Host "Evidence: $runDir"
    Write-Host "Core log: $(Join-Path $TestRoot ".integration-data\logs\$registry-core-api.out.log")"
    Write-Host "Compute API log: $(Join-Path $TestRoot ".integration-data\logs\$registry-compute-api.out.log")"
    Write-Host "Compute worker log: $(Join-Path $TestRoot ".integration-data\logs\$registry-compute-worker.out.log")"
    Write-Host "DB diagnosis: psql against verified $DatabaseName; inspect $coreSchema.trading_normalization_runs and $computeSchema.trading_review_jobs by IDs in evidence."
    exit 1
} finally {
    if ($started) {
        & (Join-Path $PSScriptRoot 'stop-integration.ps1') -RegistryId $registry `
            -Service @('core-api', 'compute-worker', 'compute-api') | Out-Null
    }
    Remove-Item Env:TRADING_RUNTIME_ACCESS_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:TRADING_RUNTIME_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TRADING_RUNTIME_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:TRADING_RUNTIME_CORE_JAR -ErrorAction SilentlyContinue
}
