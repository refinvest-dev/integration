[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$registry = Join-Path $PSScriptRoot '..\.integration-data\observability\processes.json'
if (-not (Test-Path $registry)) { throw 'No recorded observability processes.' }
$records = @(Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json)
foreach ($record in $records) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    if ($process.StartTime.ToUniversalTime().ToString('o') -ne $record.started -or $process.Path -ne $record.path) {
        throw "Process identity changed for PID $($record.id); refusing to stop it."
    }
    Stop-Process -Id $record.id
}
Remove-Item -LiteralPath $registry
