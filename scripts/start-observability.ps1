[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PrometheusExe,
    [string]$GrafanaExe,
    [securestring]$GrafanaAdminPassword,
    [switch]$PrometheusOnly,
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$Profile = 'observability-load',
    [string]$RegistryId = 'observability-load'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$values = Get-IntegrationEnvironment $EnvironmentFile
if (-not $PrometheusOnly -and ([string]::IsNullOrWhiteSpace($GrafanaExe) -or $null -eq $GrafanaAdminPassword)) {
    throw 'GrafanaExe and GrafanaAdminPassword are required unless PrometheusOnly is selected.'
}
$data = Join-Path $root '.integration-data\observability'
$registry = Join-Path $data 'grafana-process.json'
if (-not $PrometheusOnly -and (Test-Path $registry)) { throw "Grafana registry exists: $registry. Stop the recorded process first." }
$prometheus = (Resolve-Path -LiteralPath $PrometheusExe).Path
$grafana = if ($PrometheusOnly) { $null } else { (Resolve-Path -LiteralPath $GrafanaExe).Path }
$prometheusIdentity = New-PrometheusIntegrationIdentity -IntegrationPath $root -EnvironmentFile $EnvironmentFile -ExecutablePath $prometheus -Profile $Profile -RegistryId $RegistryId
$allowed = Assert-IntegrationServiceStartAllowed -RootPath $root -Name prometheus -ExpectedIdentity $prometheusIdentity -RegistryId $RegistryId -Port 9090
if ($allowed.reuse) {
    Write-Output 'Prometheus already running with matching identity: http://127.0.0.1:9090/targets'
    if (-not $PrometheusOnly) {
        throw 'Prometheus is already running; stop this observability profile before starting Prometheus and Grafana together.'
    }
    return
}
if (-not $PrometheusOnly -and (Test-IntegrationTcpListener -Port 3002)) {
    throw 'UNOWNED_PORT 3002 is already in use; refusing to start or terminate Grafana.'
}
New-Item -ItemType Directory -Force -Path $data, (Join-Path $data 'prometheus'), (Join-Path $data 'grafana'), (Join-Path $data 'logs') | Out-Null
$scrapePasswordFile = Join-Path $data 'core-metrics-password'
try {
    $scrapePassword = $values['REFINVEST_METRICS_SCRAPE_PASSWORD']
    if ([string]::IsNullOrWhiteSpace($scrapePassword)) {
        $bytes = [byte[]]::new(32)
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
        $scrapePassword = [Convert]::ToBase64String($bytes)
    }
    [IO.File]::WriteAllText($scrapePasswordFile, $scrapePassword, [Text.UTF8Encoding]::new($false))
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
    Register-IntegrationProcess -RootPath $root -Name prometheus -Process $prom -Identity $prometheusIdentity `
        -RegistryId $RegistryId -Port 9090 -HealthUrl 'http://127.0.0.1:9090/-/healthy' `
        -LogPath (Join-Path $data 'logs\prometheus.out.log')
} catch {
    Remove-Item -LiteralPath $scrapePasswordFile -ErrorAction SilentlyContinue
    throw
}
try {
    if ($PrometheusOnly) {
        Write-Output 'Prometheus: http://127.0.0.1:9090/targets'
        return
    }
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
    [pscustomobject]@{ id = $graf.Id; started = $graf.StartTime.ToUniversalTime().ToString('o'); path = $graf.Path } |
        ConvertTo-Json | Set-Content -LiteralPath $registry -Encoding UTF8
    Write-Output 'Prometheus: http://127.0.0.1:9090/targets'
    Write-Output 'Grafana: http://127.0.0.1:3002 (admin; password supplied at launch)'
} catch {
    Stop-IntegrationRegisteredProcess -RootPath $root -Name prometheus -RegistryId $RegistryId -ErrorAction SilentlyContinue | Out-Null
    Remove-Item -LiteralPath $scrapePasswordFile -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item Env:GF_SECURITY_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}
