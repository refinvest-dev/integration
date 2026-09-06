[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local')
)

$ErrorActionPreference = 'Stop'

$contents = @"
INTEGRATION_POSTGRES_HOST=127.0.0.1
INTEGRATION_POSTGRES_PORT=5432
POSTGRES_DB=refinvest_integration
POSTGRES_USER=refinvest
POSTGRES_PASSWORD=refinvest-ci-only
INTEGRATION_CORE_PORT=8080
INTEGRATION_COMPUTE_PORT=8000
INTEGRATION_MEMBER_ID=900000000000001
REFINVEST_JWT_ISSUER=https://integration.refinvest.local
REFINVEST_JWT_AUDIENCE=refinvest-integration
REFINVEST_JWT_SECRET=refinvest-ci-only-jwt-secret-at-least-32-characters
REFINVEST_WEB_ORIGIN=http://localhost:3001
REFINVEST_COOKIE_SECURE=false
REFINVEST_COOKIE_SAME_SITE=Lax
REFINVEST_INTERNAL_API_KEY=refinvest-ci-only-internal-api-key
REFINVEST_COMPUTE_ENABLED=true
REFINVEST_SNAPSHOT_STORAGE_ROOT=.integration-data/snapshots
"@

[System.IO.File]::WriteAllText($EnvironmentFile, $contents, [System.Text.UTF8Encoding]::new($false))
Write-Host "Created CI-only integration environment: $EnvironmentFile"
