[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest")
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $EnvironmentFile)) { throw "Integration environment file does not exist: $EnvironmentFile" }

Get-Content -LiteralPath $EnvironmentFile | Where-Object { $_ -match '^[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    Set-Item -Path "Env:$name" -Value $value
}
foreach ($required in 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'INTEGRATION_POSTGRES_PORT', 'REFINVEST_SNAPSHOT_STORAGE_ROOT') {
    if ([string]::IsNullOrWhiteSpace((Get-Item -Path "Env:$required" -ErrorAction SilentlyContinue).Value)) { throw "$required must be set in $EnvironmentFile" }
}

$env:REFINVEST_DATABASE_URL = "postgresql://$($env:POSTGRES_USER):$($env:POSTGRES_PASSWORD)@localhost:$($env:INTEGRATION_POSTGRES_PORT)/$($env:POSTGRES_DB)"
$env:REFINVEST_SNAPSHOT_STORAGE_ROOT = [IO.Path]::GetFullPath((Join-Path (Join-Path $PSScriptRoot '..') $env:REFINVEST_SNAPSHOT_STORAGE_ROOT))

& (Join-Path $ComputeApiPath '.venv\Scripts\python.exe') -c "from refinvest.app.integration_fixture import integration_fixture_main; integration_fixture_main()"
if ($LASTEXITCODE -ne 0) { throw 'Could not publish the Compute integration fixture.' }
