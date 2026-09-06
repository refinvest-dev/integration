[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$rootPath = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment (Join-Path $rootPath '.env.local')
Get-IntegrationServiceStatus -RootPath $rootPath -EnvironmentValues $values | Format-Table -AutoSize
