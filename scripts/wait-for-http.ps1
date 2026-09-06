[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Uri,
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    try {
        $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 5 -UseBasicParsing
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
            Write-Host "Ready: $Uri ($($response.StatusCode))"
            exit 0
        }
    } catch {
        # A service starting up commonly refuses a connection before becoming healthy.
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)

throw "Timed out waiting for HTTP readiness: $Uri"
