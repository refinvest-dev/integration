[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ContextPath = (Join-Path $PSScriptRoot '..\..\context'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [double]$SteadyRate = 0.2,
    [double]$SteadyDuration = 30,
    [double]$BurstRate = 2,
    [double]$BurstDuration = 10,
    [int]$MaxSubmissions = 30,
    [int]$Concurrency = 3,
    [double]$TerminalTimeout = 120,
    [double]$PollInterval = 2,
    [double]$Cooldown = 5
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
if ($values['POSTGRES_DB'] -cne 'refinvest_integration') {
    throw 'Load tests are allowed only against refinvest_integration.'
}
& (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') `
    -ContextPath $ContextPath -CoreApiPath $CoreApiPath -ComputeApiPath $ComputeApiPath

# Each invocation gets a separate synthetic PRO member, so the monthly quota and
# concurrent-run count of an existing developer account are untouched.
$memberId = [string](900000000000000L + ((Get-Date).ToUniversalTime().Ticks % 10000000000000L))
$token = & (Join-Path $PSScriptRoot 'seed-core-member.ps1') `
    -EnvironmentFile $EnvironmentFile -MemberId $memberId -Tier PRO | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($token)) { throw 'Could not seed a local integration member.' }
$psql = Get-IntegrationPsql
if (-not $psql) { throw 'Local psql is required for read-only integration DB verification.' }
$env:REFINVEST_LOAD_ACCESS_TOKEN = [string]$token
$env:REFINVEST_LOAD_PSQL = $psql
$env:REFINVEST_LOAD_DB_HOST = Get-IntegrationPostgresHost $values
$env:REFINVEST_LOAD_DB_PORT = $values['INTEGRATION_POSTGRES_PORT']
$env:REFINVEST_LOAD_DB_USER = $values['POSTGRES_USER']
$env:PGPASSWORD = $values['POSTGRES_PASSWORD']
try {
    $arguments = @(
        (Join-Path $PSScriptRoot 'run-load-test.py'),
        '--core-url', "http://127.0.0.1:$($values['INTEGRATION_CORE_PORT'])",
        '--compute-url', "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])",
        '--fixture-root', (Join-Path $PSScriptRoot '..\.integration-data\snapshots\integration-e2e-v1'),
        '--steady-rate', "$SteadyRate", '--steady-duration', "$SteadyDuration",
        '--burst-rate', "$BurstRate", '--burst-duration', "$BurstDuration",
        '--max-submissions', "$MaxSubmissions", '--concurrency', "$Concurrency",
        '--terminal-timeout', "$TerminalTimeout", '--poll-interval', "$PollInterval",
        '--cooldown', "$Cooldown"
    )
    & python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Load test failed; inspect the printed report path.' }
} finally {
    Remove-Item Env:REFINVEST_LOAD_ACCESS_TOKEN -ErrorAction SilentlyContinue
    foreach ($name in 'REFINVEST_LOAD_PSQL', 'REFINVEST_LOAD_DB_HOST', 'REFINVEST_LOAD_DB_PORT', 'REFINVEST_LOAD_DB_USER', 'PGPASSWORD') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    $token = $null
}
