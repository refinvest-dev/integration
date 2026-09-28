[CmdletBinding()]
param(
    [ValidateSet('compute-api', 'compute-worker', 'compute-fault-proxy', 'core-api', 'web', 'prometheus')]
    [string[]]$Service = @('compute-api', 'compute-worker', 'compute-fault-proxy', 'core-api', 'web', 'prometheus'),
    [string]$Profile = 'default',
    [string]$RegistryId = 'default',
    [switch]$Legacy
)

$ErrorActionPreference = 'Stop'
$rootPath = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
if ($Legacy) {
    Stop-IntegrationLegacyProcesses -RootPath $rootPath -Name $Service | Format-Table -AutoSize
} else {
    Stop-IntegrationRegisteredProcess -RootPath $rootPath -Name $Service -RegistryId $RegistryId | Format-Table -AutoSize
}
