[CmdletBinding()]
param([Parameter(Mandatory)][string]$EnvironmentFile,
      [Parameter(Mandatory)][string]$CorePath,
      [string]$DatabaseSchema = 'public')
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Assert-IntegrationEnvironment $values @('POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','INTEGRATION_POSTGRES_PORT')
if ($values.POSTGRES_DB -cne 'refinvest_integration') { throw 'Core trading migrations require refinvest_integration.' }
$hostName = Get-IntegrationPostgresHost $values
if ($hostName -notin @('127.0.0.1','localhost','::1')) { throw 'Core trading migrations require local PostgreSQL.' }
$psql = Get-IntegrationPsql
if (-not $psql) { throw 'Local psql is required.' }
$env:PGPASSWORD = $values.POSTGRES_PASSWORD
$args = @('-h',$hostName,'-p',$values.INTEGRATION_POSTGRES_PORT,'-U',$values.POSTGRES_USER,
          '-d',$values.POSTGRES_DB,'-v','ON_ERROR_STOP=1')
$target = (& $psql @args -tA -c 'SELECT current_database();').Trim()
if ($LASTEXITCODE -ne 0 -or $target -cne 'refinvest_integration') { throw 'Core migration target differs.' }
if ($DatabaseSchema -notmatch '^(public|tnorm_[a-z0-9]+)$') { throw 'Invalid Core integration schema.' }
& $psql @args -c "CREATE SCHEMA IF NOT EXISTS $DatabaseSchema;" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Core integration schema creation failed.' }
$previousPGOptions = $env:PGOPTIONS
try {
$env:PGOPTIONS = "-c search_path=$DatabaseSchema"
$migrationRoot = Join-Path $CorePath 'tradingreview\adapter\persistence\src\main\resources\db\migration'
$migrations = @(Get-ChildItem -LiteralPath $migrationRoot -Filter 'V*__*.sql' |
    Sort-Object { [int]($_.Name -replace '^V([0-9]+)__.*$', '$1') })
if ($migrations.Count -lt 4) { throw 'Core trading migration set is incomplete.' }
& $psql @args -c 'CREATE TABLE IF NOT EXISTS integration_core_trading_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Core migration tracking is unavailable.' }
foreach ($migration in $migrations) {
    if ($migration.Name -notmatch '^V[0-9]+__[a-z0-9_]+\.sql$') { throw 'Invalid Core migration filename.' }
    $name = $migration.Name
    $applied = [string](& $psql @args -tA -c "SELECT 1 FROM integration_core_trading_migrations WHERE filename='$name';")
    if ($null -eq $applied) { $applied = '' }
    $applied = $applied.Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect $name." }
    if ($applied -eq '1') { Write-Host "SKIP $name"; continue }
    if ($name -eq 'V1__trading_account_book.sql') {
        $existing = (& $psql @args -tA -c "SELECT to_regclass('trading_accounts') IS NOT NULL;").Trim()
        if ($existing -cne 'f') { throw 'Core trading tables exist without integration migration tracking.' }
    }
    Write-Host "APPLY $name"
    & $psql @args --single-transaction -f $migration.FullName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Core migration $name failed." }
    & $psql @args -c "INSERT INTO integration_core_trading_migrations(filename) VALUES ('$name');" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not record Core migration $name." }
}
Write-Host 'Core trading migrations are ready in the local integration database.'
} finally {
    if ($null -eq $previousPGOptions) { Remove-Item Env:PGOPTIONS -ErrorAction SilentlyContinue }
    else { $env:PGOPTIONS = $previousPGOptions }
}
