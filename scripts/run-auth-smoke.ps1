[CmdletBinding()]
param(
    [ValidateSet('RefreshRotation', 'Logout')]
    [string]$Scenario,
    [Parameter(Mandatory)]
    [string]$RefreshToken,
    [string]$CoreBaseUrl = 'http://localhost:8080'
)

$ErrorActionPreference = 'Stop'
if ($CoreBaseUrl -notmatch '^http://localhost(:\d+)?$') { throw 'CoreBaseUrl must use localhost for this Lax SameSite smoke.' }

$workDir = Join-Path $env:TEMP "refinvest-auth-smoke-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $workDir | Out-Null
$cookieJar = Join-Path $workDir 'cookies.txt'
$headers = Join-Path $workDir 'headers.txt'
$body = Join-Path $workDir 'body.txt'

function Invoke-Curl([string[]]$Arguments) {
    Remove-Item -LiteralPath $headers, $body -Force -ErrorAction SilentlyContinue
    $status = & curl.exe '-sS' '-o' $body '-D' $headers '-w' '%{http_code}' @Arguments
    if ($LASTEXITCODE -ne 0) { throw "curl failed: $status" }
    return [int]$status
}

function Get-CsrfToken {
    $token = Get-Content -LiteralPath $cookieJar | ForEach-Object {
        $fields = $_ -split "`t"
        if ($fields.Count -ge 7 -and $fields[-2] -eq 'XSRF-TOKEN') { $fields[-1] }
    } | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Core did not issue an XSRF-TOKEN cookie.' }
    return $token
}

function Get-SetCookieEvidence {
    @(Get-Content -LiteralPath $headers | Where-Object { $_ -match '^Set-Cookie:' } | ForEach-Object {
        $_ -replace '(REFINVEST_(?:ACCESS|REFRESH)_TOKEN=)[^;]*', '$1<redacted>'
    })
}

try {
    $csrfStatus = Invoke-Curl @('--cookie-jar', $cookieJar, "$CoreBaseUrl/auth/csrf")
    if ($csrfStatus -ne 204) { throw "Expected GET /auth/csrf to return 204, got $csrfStatus." }
    $csrf = Get-CsrfToken

    $missingCsrfStatus = Invoke-Curl @('-X', 'POST', '-H', "Cookie: REFINVEST_REFRESH_TOKEN=$RefreshToken", "$CoreBaseUrl/auth/refresh")
    if ($missingCsrfStatus -ne 403) { throw "Expected refresh without CSRF to return 403, got $missingCsrfStatus." }

    if ($Scenario -eq 'RefreshRotation') {
        $refreshStatus = Invoke-Curl @('-X', 'POST', '--cookie', "REFINVEST_REFRESH_TOKEN=$RefreshToken; XSRF-TOKEN=$csrf", '--cookie-jar', $cookieJar, '-H', "X-XSRF-TOKEN: $csrf", "$CoreBaseUrl/auth/refresh")
        $refreshEvidence = Get-SetCookieEvidence
        if ($refreshStatus -ne 204 -or (Get-Content -LiteralPath $body -Raw).Length -ne 0) { throw "Expected refresh to return empty 204, got $refreshStatus." }
        if ($refreshEvidence.Count -lt 2) { throw 'Refresh did not set both authentication cookies.' }
        $rotatedRefreshToken = Get-Content -LiteralPath $cookieJar | ForEach-Object {
            $fields = $_ -split "`t"
            if ($fields.Count -ge 7 -and $fields[-2] -eq 'REFINVEST_REFRESH_TOKEN') { $fields[-1] }
        } | Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($rotatedRefreshToken)) { throw 'Refresh did not rotate the refresh cookie.' }

        $replayStatus = Invoke-Curl @('-X', 'POST', '-H', "Cookie: REFINVEST_REFRESH_TOKEN=$RefreshToken; XSRF-TOKEN=$csrf", '-H', "X-XSRF-TOKEN: $csrf", "$CoreBaseUrl/auth/refresh")
        if ($replayStatus -ne 401) { throw "Expected old refresh token replay to return 401, got $replayStatus." }
        $familyStatus = Invoke-Curl @('-X', 'POST', '-H', "Cookie: REFINVEST_REFRESH_TOKEN=$rotatedRefreshToken; XSRF-TOKEN=$csrf", '-H', "X-XSRF-TOKEN: $csrf", "$CoreBaseUrl/auth/refresh")
        if ($familyStatus -ne 401) { throw "Expected rotated token after replay detection to return 401, got $familyStatus." }
        [pscustomobject]@{ scenario = $Scenario; csrfWithoutHeader = $missingCsrfStatus; refresh = $refreshStatus; oldTokenReplay = $replayStatus; familyAfterReplay = $familyStatus; setCookie = $refreshEvidence }
        return
    }

    $logoutStatus = Invoke-Curl @('-X', 'POST', '-H', "Cookie: REFINVEST_REFRESH_TOKEN=$RefreshToken; XSRF-TOKEN=$csrf", '-H', "X-XSRF-TOKEN: $csrf", "$CoreBaseUrl/auth/logout")
    $logoutEvidence = Get-SetCookieEvidence
    if ($logoutStatus -ne 204) { throw "Expected logout to return 204, got $logoutStatus." }
    if (($logoutEvidence | Where-Object { $_ -match 'REFINVEST_ACCESS_TOKEN=<redacted>;.*Max-Age=0' }).Count -ne 1 -or ($logoutEvidence | Where-Object { $_ -match 'REFINVEST_REFRESH_TOKEN=<redacted>;.*Max-Age=0' }).Count -ne 1) { throw 'Logout did not clear both authentication cookies.' }
    $afterLogoutStatus = Invoke-Curl @('-X', 'POST', '-H', "Cookie: REFINVEST_REFRESH_TOKEN=$RefreshToken; XSRF-TOKEN=$csrf", '-H', "X-XSRF-TOKEN: $csrf", "$CoreBaseUrl/auth/refresh")
    if ($afterLogoutStatus -ne 401) { throw "Expected refresh after logout to return 401, got $afterLogoutStatus." }
    [pscustomobject]@{ scenario = $Scenario; csrfWithoutHeader = $missingCsrfStatus; logout = $logoutStatus; refreshAfterLogout = $afterLogoutStatus; setCookie = $logoutEvidence }
}
finally {
    if (Test-Path -LiteralPath $workDir) { Remove-Item -LiteralPath $workDir -Recurse -Force }
}
