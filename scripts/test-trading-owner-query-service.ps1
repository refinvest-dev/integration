[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '../.env.local'),
    [string]$ContextPath = (Join-Path $PSScriptRoot '../../context'),
    [string]$CorePath = (Join-Path $PSScriptRoot '../../api/refinvest'),
    [string]$GoPath = (Join-Path $PSScriptRoot '../../trading-review-api/trading-review-api'),
    [string]$ComputePath = (Join-Path $PSScriptRoot '../../compute-api/refinvest'),
    [string]$WebPath = (Join-Path $PSScriptRoot '../../web/web'),
    [string]$PythonPath = 'python',
    [string]$JavaPath,
    [string]$GoBinary,
    [ValidateSet('main', 'fixture', 'fleet', 'transport')][string]$CoreMode = 'main',
    [string]$CoreJar,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$arguments = @('-B', (Join-Path $PSScriptRoot 'owner_query_e2e.py'), '--environment-file', $EnvironmentFile,
    '--context-path', $ContextPath, '--core-path', $CorePath, '--go-path', $GoPath,
    '--compute-path', $ComputePath, '--web-path', $WebPath, '--core-mode', $CoreMode)
if ($JavaPath) { $arguments += @('--java', $JavaPath) }
if ($GoBinary) { $arguments += @('--go-binary', $GoBinary) }
if ($CoreJar) { $arguments += @('--core-jar', $CoreJar) }
if ($SkipBuild) { $arguments += '--skip-build' }
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw 'Owner query service acceptance failed; inspect the run evidence.' }
