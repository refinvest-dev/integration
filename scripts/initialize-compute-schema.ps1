[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComposeFile = (Join-Path $PSScriptRoot "..\compose.yaml"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvironmentFile)) { throw "Integration environment file does not exist: $EnvironmentFile" }

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
foreach ($required in 'POSTGRES_DB', 'POSTGRES_USER') {
    if (-not $values.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($values[$required])) { throw "$required must be set in $EnvironmentFile" }
}

$psql = Get-IntegrationPsql
if ($psql) {
    Assert-IntegrationEnvironment $values @('POSTGRES_PASSWORD', 'INTEGRATION_POSTGRES_PORT')
    $env:PGPASSWORD = $values.POSTGRES_PASSWORD
    $hostName = Get-IntegrationPostgresHost $values
    $arguments = @('-h', $hostName, '-p', $values.INTEGRATION_POSTGRES_PORT, '-U', $values.POSTGRES_USER, '-d', $values.POSTGRES_DB, '-v', 'ON_ERROR_STOP=1')
    & $psql @arguments -c 'CREATE TABLE IF NOT EXISTS integration_schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);'
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize integration migration metadata.' }

    $migrations = Get-ChildItem (Join-Path $ComputeApiPath 'migrations') -Filter '*.sql' | Sort-Object Name
    if ($migrations.Count -eq 0) { throw "No Compute migrations found under $ComputeApiPath" }
    foreach ($migration in $migrations) {
        $applied = & $psql @arguments -tA -c "SELECT 1 FROM integration_schema_migrations WHERE filename = '$($migration.Name)';"
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect $($migration.Name)." }
        if ($applied -match '1') { Write-Host "SKIP $($migration.Name)"; continue }
        Write-Host "APPLY $($migration.Name)"
        & $psql @arguments -f $migration.FullName
        if ($LASTEXITCODE -ne 0) { throw "Could not apply $($migration.Name)." }
        & $psql @arguments -c "INSERT INTO integration_schema_migrations (filename) VALUES ('$($migration.Name)');"
        if ($LASTEXITCODE -ne 0) { throw "Could not record $($migration.Name)." }
    }
    Write-Host 'Compute migrations are ready for the local integration database.'
    return
}

if (-not (Test-Path -LiteralPath $ComposeFile)) { throw "Docker Compose file does not exist: $ComposeFile" }

$compose = @('compose', '--env-file', $EnvironmentFile, '-f', $ComposeFile)
& docker @compose 'exec' '-T' 'postgres' 'psql' '-v' 'ON_ERROR_STOP=1' '-U' $values.POSTGRES_USER '-d' $values.POSTGRES_DB '-c' 'CREATE TABLE IF NOT EXISTS integration_schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);'
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize integration migration metadata.' }

$migrations = Get-ChildItem (Join-Path $ComputeApiPath 'migrations') -Filter '*.sql' | Sort-Object Name
if ($migrations.Count -eq 0) { throw "No Compute migrations found under $ComputeApiPath" }
foreach ($migration in $migrations) {
    $applied = & docker @compose 'exec' '-T' 'postgres' 'psql' '-tA' '-U' $values.POSTGRES_USER '-d' $values.POSTGRES_DB '-c' "SELECT 1 FROM integration_schema_migrations WHERE filename = '$($migration.Name)';"
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect $($migration.Name)." }
    if ($applied -match '1') { Write-Host "SKIP $($migration.Name)"; continue }
    Write-Host "APPLY $($migration.Name)"
    Get-Content -LiteralPath $migration.FullName -Raw | & docker @compose 'exec' '-T' 'postgres' 'psql' '-v' 'ON_ERROR_STOP=1' '-U' $values.POSTGRES_USER '-d' $values.POSTGRES_DB
    if ($LASTEXITCODE -ne 0) { throw "Could not apply $($migration.Name)." }
    & docker @compose 'exec' '-T' 'postgres' 'psql' '-v' 'ON_ERROR_STOP=1' '-U' $values.POSTGRES_USER '-d' $values.POSTGRES_DB '-c' "INSERT INTO integration_schema_migrations (filename) VALUES ('$($migration.Name)');"
    if ($LASTEXITCODE -ne 0) { throw "Could not record $($migration.Name)." }
}

Write-Host 'Compute migrations are ready for the local integration database.'
