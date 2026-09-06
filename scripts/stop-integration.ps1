[CmdletBinding()]
param(
    [ValidateSet('compute-api', 'compute-worker', 'core-api', 'web')]
    [string[]]$Service = @('compute-api', 'compute-worker', 'core-api', 'web')
)

$ErrorActionPreference = 'Stop'
$rootPath = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
Stop-IntegrationRegisteredProcess -RootPath $rootPath -Name $Service | Format-Table -AutoSize
