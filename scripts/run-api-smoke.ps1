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
$curl = @(
    (Get-Command curl.exe -ErrorAction SilentlyContinue).Source,
    (Get-Command curl -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($curl)) { throw 'curl must be available to run the API smoke.' }

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
        $response = & $curl @arguments
        if ($LASTEXITCODE -ne 0) { throw "API request failed: $Method $Path`n$response" }
        $response | ConvertFrom-Json
    } finally {
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue }
    }
}

function Wait-ForCompletedBacktest([string]$RunId) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        $status = Invoke-Json GET "/backtest-runs/$RunId"
    } while ($status.status -in @('PENDING', 'RUNNING') -and (Get-Date) -lt $deadline)

    if ($status.status -ne 'COMPLETED') {
        throw "Backtest did not complete: $($status | ConvertTo-Json -Depth 8)"
    }

    if ($status.datasetSnapshotId -ne 'integration-e2e-v1') {
        throw 'Completed run did not preserve the integration fixture snapshot.'
    }

    return $status
}

function Get-RequiredProperty([object]$Object, [string]$Property, [string]$Context) {
    if ($null -eq $Object) { throw "$Context is missing." }
    $value = $Object.PSObject.Properties[$Property]
    if ($null -eq $value) { throw "$Context.$Property is missing." }
    return $value.Value
}

function Assert-DateSequence([object]$Curve, [string[]]$ExpectedDates, [string]$Context) {
    if ($null -eq $Curve) { throw "$Context is missing." }
    $actualDates = @($Curve | ForEach-Object { $_.date })
    $actual = $actualDates -join ','
    $expected = $ExpectedDates -join ','
    if ($actual -cne $expected) {
        throw "$Context dates differ. Expected [$expected], actual [$actual]."
    }
}

function Assert-VisualizationResult(
    [object]$Status,
    [string]$ExpectedRelation,
    [string]$ExpectedExecutionAsset,
    [string]$ExpectedSignalAsset,
    [string[]]$ExpectedPrimaryDates,
    [string[]]$ExpectedSecondaryDates,
    [bool]$RequireTrades
) {
    $result = Get-RequiredProperty $Status 'result' 'Completed run'
    foreach ($property in @(
        'metrics', 'equityCurve', 'trades', 'benchmark', 'signalExecutionMarketRelation',
        'signalExecutionDelay', 'sampleSizeWarning', 'dataIntegrityStatus'
    )) {
        $null = Get-RequiredProperty $result $property 'Backtest result'
    }

    if ($result.signalExecutionMarketRelation -cne $ExpectedRelation) {
        throw "Expected signalExecutionMarketRelation=$ExpectedRelation, actual=$($result.signalExecutionMarketRelation)."
    }

    $benchmark = Get-RequiredProperty $result 'benchmark' 'Backtest result'
    $primary = Get-RequiredProperty $benchmark 'primary' 'Backtest result.benchmark'
    if ($primary.asset -cne $ExpectedExecutionAsset) {
        throw "Expected primary benchmark asset=$ExpectedExecutionAsset, actual=$($primary.asset)."
    }
    Assert-DateSequence $primary.equityCurve $ExpectedPrimaryDates 'Primary benchmark equityCurve'

    $secondaryProperty = $benchmark.PSObject.Properties['secondaryReference']
    if ($ExpectedSecondaryDates.Count -eq 0) {
        if ($null -ne $secondaryProperty -and $null -ne $secondaryProperty.Value) {
            throw 'Same-asset scenario must not return a secondary benchmark reference.'
        }
    } else {
        if ($null -eq $secondaryProperty -or $null -eq $secondaryProperty.Value) {
            throw 'Cross-market scenario did not return a secondary benchmark reference.'
        }
        $secondary = $secondaryProperty.Value
        if ($secondary.asset -cne $ExpectedSignalAsset) {
            throw "Expected secondary benchmark asset=$ExpectedSignalAsset, actual=$($secondary.asset)."
        }
        Assert-DateSequence $secondary.equityCurve $ExpectedSecondaryDates 'Secondary benchmark equityCurve'
    }

    if ($RequireTrades) {
        if (@($result.trades).Count -eq 0) { throw 'Expected the Cross-Market scenario to realize at least one trade.' }
        if (@($result.equityCurve).Count -eq 0) { throw 'Expected the Cross-Market scenario to return a strategy equity curve.' }
        foreach ($trade in @($result.trades)) {
            foreach ($property in @('signalTime', 'entryTime', 'exitTime', 'entryPrice', 'exitPrice', 'returnPct')) {
                $null = Get-RequiredProperty $trade $property 'Cross-Market trade'
            }
        }
        if (@($result.signalExecutionDelay.distribution).Count -ne @($result.trades).Count) {
            throw 'Signal-to-execution delay distribution must have one entry per realized trade.'
        }
    }
}

function Assert-NegativeStrategyDrawdown([object]$Status) {
    $result = Get-RequiredProperty $Status 'result' 'Completed run'
    $metrics = Get-RequiredProperty $result 'metrics' 'Backtest result'
    $maximumDrawdown = [decimal](Get-RequiredProperty $metrics 'mdd' 'Backtest result.metrics')
    if ($maximumDrawdown -ge 0) {
        throw "Expected a negative strategy maximum drawdown, actual=$maximumDrawdown."
    }

    $runningPeak = [decimal]0
    $hasNegativeDrawdownPoint = $false
    foreach ($point in @(Get-RequiredProperty $result 'equityCurve' 'Backtest result')) {
        $equity = [decimal](Get-RequiredProperty $point 'value' 'Strategy equityCurve point')
        $runningPeak = [decimal]::Max($runningPeak, $equity)
        if (($equity / $runningPeak) - 1 -lt 0) {
            $hasNegativeDrawdownPoint = $true
            break
        }
    }
    if (-not $hasNegativeDrawdownPoint) {
        throw 'Strategy equityCurve did not contain a point below its running peak.'
    }
}

# Preserve the original trade-free same-market path. It verifies empty-state results
# still include the benchmark and required visualization contract fields.
$sameMarketStrategy = Invoke-Json POST '/strategies' @{ name = "Integration same-market smoke $(Get-Date -Format yyyyMMddHHmmss)" }
$sameMarketVersion = Invoke-Json POST "/strategies/$($sameMarketStrategy.id)/versions" @{
    primarySignalAsset = 'QQQ'; executionAsset = 'QQQ'; lag = 0
    # Keep the fixture run trade-free so the final signal does not need an execution session beyond the fixture range.
    conditions = @(@{ operator = 'GT'; logicalCombinator = $null; operandA = @{ asset = 'QQQ'; metric = 'SIMPLE'; window = $null }; operandB = 1000 })
    exit = @{ holdingSignalSessions = 1 }
}
$sameMarketRun = Invoke-Json POST "/strategy-versions/$($sameMarketVersion.id)/backtests" @{ period = @{ start = '2025-01-02'; end = '2025-01-08' }; feeModel = @{ commission = 0; slippage = 0 } }
$sameMarketStatus = Wait-ForCompletedBacktest $sameMarketRun.id
$sameMarketAssertion = @{
    Status = $sameMarketStatus
    ExpectedRelation = 'SAME_MARKET'
    ExpectedExecutionAsset = 'QQQ'
    ExpectedSignalAsset = 'QQQ'
    ExpectedPrimaryDates = @('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    ExpectedSecondaryDates = [string[]]@()
    RequireTrades = $false
}
Assert-VisualizationResult @sameMarketAssertion

# BTCUSDT has continuous UTC sessions while TQQQ has US-equity sessions only.
# Exact, distinct date sequences prove that Core persists Compute's observed
# sessions instead of forward-filling either benchmark curve for display.
$crossMarketStrategy = Invoke-Json POST '/strategies' @{ name = "Integration cross-market smoke $(Get-Date -Format yyyyMMddHHmmss)" }
$crossMarketVersion = Invoke-Json POST "/strategies/$($crossMarketStrategy.id)/versions" @{
    primarySignalAsset = 'BTCUSDT'; executionAsset = 'TQQQ'; lag = 0
    # The deterministic BTCUSDT close is strictly between 90,000 and 92,000 only on Jan 3.
    # It enters TQQQ on Jan 6 and exits on Jan 7, both inside the fixed fixture range.
    conditions = @(
        @{ operator = 'GT'; logicalCombinator = $null; operandA = @{ asset = 'BTCUSDT'; metric = 'SIMPLE'; window = $null }; operandB = 90000 },
        @{ operator = 'LT'; logicalCombinator = 'AND'; operandA = @{ asset = 'BTCUSDT'; metric = 'SIMPLE'; window = $null }; operandB = 92000 }
    )
    exit = @{ holdingSignalSessions = 3 }
}
$crossMarketRun = Invoke-Json POST "/strategy-versions/$($crossMarketVersion.id)/backtests" @{ period = @{ start = '2025-01-02'; end = '2025-01-08' }; feeModel = @{ commission = 0; slippage = 0 } }
$crossMarketStatus = Wait-ForCompletedBacktest $crossMarketRun.id
$crossMarketAssertion = @{
    Status = $crossMarketStatus
    ExpectedRelation = 'CROSS_MARKET'
    ExpectedExecutionAsset = 'TQQQ'
    ExpectedSignalAsset = 'BTCUSDT'
    ExpectedPrimaryDates = @('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    ExpectedSecondaryDates = @('2025-01-02', '2025-01-03', '2025-01-04', '2025-01-05', '2025-01-06', '2025-01-07', '2025-01-08')
    RequireTrades = $true
}
Assert-VisualizationResult @crossMarketAssertion

# Keep the immutable price fixture unchanged while making a deterministic
# strategy-equity drawdown. The 2% entry/exit commission exceeds the fixture's
# one-session TQQQ gain, so the realized curve has a negative drawdown point.
$drawdownStrategy = Invoke-Json POST '/strategies' @{ name = "Integration drawdown smoke $(Get-Date -Format yyyyMMddHHmmss)" }
$drawdownVersion = Invoke-Json POST "/strategies/$($drawdownStrategy.id)/versions" @{
    primarySignalAsset = 'BTCUSDT'; executionAsset = 'TQQQ'; lag = 0
    conditions = @(
        @{ operator = 'GT'; logicalCombinator = $null; operandA = @{ asset = 'BTCUSDT'; metric = 'SIMPLE'; window = $null }; operandB = 90000 },
        @{ operator = 'LT'; logicalCombinator = 'AND'; operandA = @{ asset = 'BTCUSDT'; metric = 'SIMPLE'; window = $null }; operandB = 92000 }
    )
    exit = @{ holdingSignalSessions = 3 }
}
$drawdownRun = Invoke-Json POST "/strategy-versions/$($drawdownVersion.id)/backtests" @{
    period = @{ start = '2025-01-02'; end = '2025-01-08' }
    feeModel = @{ commission = 0.02; slippage = 0 }
}
$drawdownStatus = Wait-ForCompletedBacktest $drawdownRun.id
$drawdownAssertion = @{
    Status = $drawdownStatus
    ExpectedRelation = 'CROSS_MARKET'
    ExpectedExecutionAsset = 'TQQQ'
    ExpectedSignalAsset = 'BTCUSDT'
    ExpectedPrimaryDates = @('2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08')
    ExpectedSecondaryDates = @('2025-01-02', '2025-01-03', '2025-01-04', '2025-01-05', '2025-01-06', '2025-01-07', '2025-01-08')
    RequireTrades = $true
}
Assert-VisualizationResult @drawdownAssertion
Assert-NegativeStrategyDrawdown $drawdownStatus

[pscustomobject]@{
    sameMarket = $sameMarketStatus
    crossMarket = $crossMarketStatus
    drawdown = $drawdownStatus
} | ConvertTo-Json -Depth 10
