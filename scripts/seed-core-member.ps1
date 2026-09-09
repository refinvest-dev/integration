[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$MemberId,
    [ValidateSet('FREE', 'PRO')]
    [string]$Tier = 'FREE'
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$values.Keys | ForEach-Object { Set-Item "Env:$_" $values[$_] }
$memberId = if ($MemberId) { $MemberId } elseif ($env:INTEGRATION_MEMBER_ID) { $env:INTEGRATION_MEMBER_ID } else { '900000000000001' }
if ($memberId -notmatch '^\d+$') { throw 'Integration member ID must be a positive numeric identifier.' }
$env:INTEGRATION_MEMBER_ID = $memberId
$sql = "INSERT INTO members (id, role, created_at) VALUES ($memberId, 'MEMBER', CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING; INSERT INTO subscriptions (member_id, tier) VALUES ($memberId, '$Tier') ON CONFLICT (member_id) DO UPDATE SET tier = EXCLUDED.tier;"
$psql = Get-IntegrationPsql

if ($psql) {
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
    & $psql -h (Get-IntegrationPostgresHost $values) -p $env:INTEGRATION_POSTGRES_PORT -U $env:POSTGRES_USER -d $env:POSTGRES_DB -v ON_ERROR_STOP=1 -c $sql
} else {
    docker compose --env-file $EnvironmentFile -f (Join-Path $PSScriptRoot '..\compose.yaml') exec -T postgres psql -v ON_ERROR_STOP=1 -U $env:POSTGRES_USER -d $env:POSTGRES_DB -c $sql
}
if ($LASTEXITCODE -ne 0) { throw 'Core tables are not ready. Start Core once before seeding.' }
& python (Join-Path $PSScriptRoot 'mint-access-token.py')
