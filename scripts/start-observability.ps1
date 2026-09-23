[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PrometheusExe,
    [Parameter(Mandatory)][string]$GrafanaExe,
    [Parameter(Mandatory)][securestring]$GrafanaAdminPassword,
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$values = Get-IntegrationEnvironment $EnvironmentFile
Assert-IntegrationEnvironment $values @('REFINVEST_METRICS_SCRAPE_PASSWORD')
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
$scrapePasswordFile = Join-Path $data 'core-metrics-password'
try {
    [IO.File]::WriteAllText($scrapePasswordFile, $values['REFINVEST_METRICS_SCRAPE_PASSWORD'], [Text.UTF8Encoding]::new($false))
    if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
        $acl = Get-Acl -LiteralPath $scrapePasswordFile
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        ))
        Set-Acl -LiteralPath $scrapePasswordFile -AclObject $acl
    }
} catch {
    Remove-Item -LiteralPath $scrapePasswordFile -ErrorAction SilentlyContinue
    throw
}

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
try {
    $prom = Start-Process @start
} catch {
    Remove-Item -LiteralPath $scrapePasswordFile -ErrorAction SilentlyContinue
    throw
}
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
    Remove-Item -LiteralPath $scrapePasswordFile -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item Env:GF_SECURITY_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}
