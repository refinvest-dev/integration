[CmdletBinding()]
param(
    [string]$Profile = 'observability-load',
    [string]$RegistryId = 'observability-load'
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
Stop-IntegrationRegisteredProcess -RootPath $root -Name prometheus -RegistryId $RegistryId | Out-Host
$registry = Join-Path $root '.integration-data\observability\grafana-process.json'
$records = if (Test-Path -LiteralPath $registry) { @(Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json) } else { @() }
foreach ($record in $records) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    if ($process.StartTime.ToUniversalTime().ToString('o') -ne $record.started -or $process.Path -ne $record.path) {
        throw "Process identity changed for PID $($record.id); refusing to stop it."
    }
    Stop-Process -Id $record.id
}
Remove-Item -LiteralPath $registry -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $PSScriptRoot '..\.integration-data\observability\core-metrics-password') -ErrorAction SilentlyContinue
