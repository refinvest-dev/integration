[CmdletBinding()]
param(
    [string]$CoreBaseUrl = 'http://localhost:8080',
    [string]$AccessToken,
    [int]$TimeoutSeconds = 90
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($AccessToken)) { throw 'Pass the token printed by seed-core-member.ps1 as -AccessToken.' }

$csrf = 'integration-smoke-csrf'
$cookie = "REFINVEST_ACCESS_TOKEN=$AccessToken; XSRF-TOKEN=$csrf"

function Invoke-Json([string]$Method, [string]$Path, [object]$Body = $null) {
    $bodyFile = $null
    try {
        $arguments = @(
            '-sS', '--fail-with-body', '--connect-timeout', '5', '--max-time', '15',
            '-X', $Method, "$CoreBaseUrl$Path",
            '-H', "Cookie: $cookie",
            '-H', "X-XSRF-TOKEN: $csrf",
            '-H', 'Content-Type: application/json'
        )
        if ($null -ne $Body) {
            $bodyFile = Join-Path ([IO.Path]::GetTempPath()) "refinvest-smoke-$([guid]::NewGuid()).json"
            [System.IO.File]::WriteAllText(
                $bodyFile,
                ($Body | ConvertTo-Json -Depth 10),
                [System.Text.UTF8Encoding]::new($false)
            )
            $arguments += '--data-binary', "@$bodyFile"
        }
        $response = & curl.exe @arguments
        if ($LASTEXITCODE -ne 0) { throw "API request failed: $Method $Path`n$response" }
        $response | ConvertFrom-Json
    } finally {
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue }
    }
}

$strategy = Invoke-Json POST '/strategies' @{ name = "Integration smoke $(Get-Date -Format yyyyMMddHHmmss)" }
$version = Invoke-Json POST "/strategies/$($strategy.id)/versions" @{
    primarySignalAsset = 'QQQ'; executionAsset = 'QQQ'; lag = 0
    # Keep the fixture run trade-free so the final signal does not need an execution session beyond the fixture range.
    conditions = @(@{ operator = 'GT'; logicalCombinator = $null; operandA = @{ asset = 'QQQ'; metric = 'SIMPLE'; window = $null }; operandB = 1000 })
    exit = @{ holdingSignalSessions = 1 }
}
$run = Invoke-Json POST "/strategy-versions/$($version.id)/backtests" @{ period = @{ start = '2025-01-02'; end = '2025-01-08' }; feeModel = @{ commission = 0; slippage = 0 } }
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 2
    $status = Invoke-Json GET "/backtest-runs/$($run.id)"
} while ($status.status -in @('PENDING', 'RUNNING') -and (Get-Date) -lt $deadline)
if ($status.status -ne 'COMPLETED') { throw "Backtest did not complete: $($status | ConvertTo-Json -Depth 8)" }
if ($null -eq $status.result -or $status.datasetSnapshotId -ne 'integration-e2e-v1') { throw 'Completed run did not preserve the integration fixture result.' }
$status | ConvertTo-Json -Depth 10
