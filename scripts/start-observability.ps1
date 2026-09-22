[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PrometheusExe,
    [Parameter(Mandatory)][string]$GrafanaExe,
    [Parameter(Mandatory)][securestring]$GrafanaAdminPassword
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$data = Join-Path $root '.integration-data\observability'
$registry = Join-Path $data 'processes.json'
if (Test-Path $registry) { throw "Observability registry exists: $registry. Stop the recorded processes first." }
foreach ($port in 9090, 3002) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use."
    }
}
$prometheus = (Resolve-Path -LiteralPath $PrometheusExe).Path
$grafana = (Resolve-Path -LiteralPath $GrafanaExe).Path
New-Item -ItemType Directory -Force -Path $data, (Join-Path $data 'prometheus'), (Join-Path $data 'grafana'), (Join-Path $data 'logs') | Out-Null

$prometheusArgs = @(
    "--config.file=$root\observability\prometheus.yml",
    "--storage.tsdb.path=$data\prometheus",
    '--storage.tsdb.retention.time=2d',
    '--web.listen-address=127.0.0.1:9090'
)
$start = @{
    FilePath = $prometheus
    ArgumentList = $prometheusArgs
    WorkingDirectory = $root
    RedirectStandardOutput = (Join-Path $data 'logs\prometheus.out.log')
    RedirectStandardError = (Join-Path $data 'logs\prometheus.err.log')
    PassThru = $true
    WindowStyle = 'Hidden'
}
$prom = Start-Process @start
try {
    $env:GF_PATHS_DATA = Join-Path $data 'grafana'
    $env:GF_PATHS_LOGS = Join-Path $data 'logs'
    $env:GF_PATHS_PLUGINS = Join-Path $data 'grafana\plugins'
    $env:GF_PATHS_PROVISIONING = Join-Path $root 'observability\grafana\provisioning'
    $env:GF_SERVER_HTTP_ADDR = '127.0.0.1'
    $env:GF_SERVER_HTTP_PORT = '3002'
    $env:GF_SECURITY_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new('', $GrafanaAdminPassword).Password
    $grafanaHome = Split-Path -Parent (Split-Path -Parent $grafana)
    $grafanaArgs = @('server', "--homepath=$grafanaHome")
    $graf = Start-Process -FilePath $grafana -ArgumentList $grafanaArgs -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $data 'logs\grafana.out.log') `
        -RedirectStandardError (Join-Path $data 'logs\grafana.err.log') -PassThru -WindowStyle Hidden
    $records = @($prom, $graf) | ForEach-Object {
        [pscustomobject]@{ id = $_.Id; started = $_.StartTime.ToUniversalTime().ToString('o'); path = $_.Path }
    }
    $records | ConvertTo-Json | Set-Content -LiteralPath $registry -Encoding UTF8
    Write-Output 'Prometheus: http://127.0.0.1:9090/targets'
    Write-Output 'Grafana: http://127.0.0.1:3002 (admin; password supplied at launch)'
} catch {
    Stop-Process -Id $prom.Id -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item Env:GF_SECURITY_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}
