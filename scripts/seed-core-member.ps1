[CmdletBinding()]
param([string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"))
$ErrorActionPreference = 'Stop'
Get-Content $EnvironmentFile | Where-Object { $_ -match '^[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object { $n,$v=$_ -split '=',2; Set-Item "Env:$n" $v }
$memberId = if ($env:INTEGRATION_MEMBER_ID) { $env:INTEGRATION_MEMBER_ID } else { '900000000000001' }
$env:INTEGRATION_MEMBER_ID = $memberId
$sql = "INSERT INTO members (id, role, created_at) VALUES ($memberId, 'MEMBER', CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING; INSERT INTO subscriptions (member_id, tier) VALUES ($memberId, 'FREE') ON CONFLICT (member_id) DO NOTHING;"
$psql = @(
    (Get-Command psql.exe -ErrorAction SilentlyContinue).Source,
    'C:\Program Files\PostgreSQL\17\bin\psql.exe'
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($psql) {
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
    & $psql -h localhost -p $env:INTEGRATION_POSTGRES_PORT -U $env:POSTGRES_USER -d $env:POSTGRES_DB -v ON_ERROR_STOP=1 -c $sql
} else {
    docker compose --env-file $EnvironmentFile -f (Join-Path $PSScriptRoot '..\compose.yaml') exec -T postgres psql -v ON_ERROR_STOP=1 -U $env:POSTGRES_USER -d $env:POSTGRES_DB -c $sql
}
if ($LASTEXITCODE -ne 0) { throw 'Core tables are not ready. Start Core once before seeding.' }
& python (Join-Path $PSScriptRoot 'mint-access-token.py')
