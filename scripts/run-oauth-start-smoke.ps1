[CmdletBinding()]
param(
    [string]$CoreBaseUrl = 'http://localhost:8080'
)

$ErrorActionPreference = 'Stop'
if ($CoreBaseUrl -notmatch '^http://localhost(:\d+)?$') { throw 'CoreBaseUrl must use localhost for this integration smoke.' }

$expectedAuthorizationOrigins = @{
    kakao = 'https://kauth.kakao.com/oauth/authorize'
    naver = 'https://nid.naver.com/oauth2.0/authorize'
    google = 'https://accounts.google.com/o/oauth2/v2/auth'
}
$workDir = Join-Path $env:TEMP "refinvest-oauth-smoke-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $workDir | Out-Null

function Invoke-AuthorizationStart([string]$Provider, [string]$ReturnTo) {
    $headers = Join-Path $workDir "$Provider-$([guid]::NewGuid()).headers"
    $cookies = Join-Path $workDir "$Provider-$([guid]::NewGuid()).cookies"
    try {
        $status = & curl.exe '-sS' '-o' 'NUL' '-D' $headers '-w' '%{http_code}' '--cookie-jar' $cookies '-G' '--data-urlencode' "returnTo=$ReturnTo" "$CoreBaseUrl/oauth2/authorization/$Provider"
        if ($LASTEXITCODE -ne 0) { throw "curl failed for ${Provider}: $status" }
        $location = Get-Content -LiteralPath $headers | Where-Object { $_ -match '^Location:' } | Select-Object -First 1
        return [pscustomobject]@{ status = [int]$status; location = ($location -replace '^Location:\s*', '') }
    } finally {
        Remove-Item -LiteralPath $headers, $cookies -Force -ErrorAction SilentlyContinue
    }
}

try {
    $normalResults = foreach ($provider in $expectedAuthorizationOrigins.Keys) {
        $result = Invoke-AuthorizationStart $provider '/strategies?tab=active'
        if ($result.status -ne 302) { throw "Expected $provider authorization start to return 302, got $($result.status)." }
        if (-not $result.location.StartsWith($expectedAuthorizationOrigins[$provider])) { throw "Unexpected $provider authorization endpoint: $($result.location)" }
        if ($result.location -notmatch '[?&]state=') { throw "$provider authorization redirect did not contain OAuth state." }
        [pscustomobject]@{ provider = $provider; status = $result.status; authorizationOrigin = $expectedAuthorizationOrigins[$provider]; statePresent = $true }
    }

    $unsafeResults = foreach ($returnTo in 'https://example.invalid/path', '//example.invalid/path', '/\example.invalid/path') {
        $result = Invoke-AuthorizationStart 'google' $returnTo
        if ($result.status -ne 400) { throw "Expected unsafe returnTo '$returnTo' to return 400, got $($result.status)." }
        [pscustomobject]@{ returnTo = $returnTo; status = $result.status }
    }

    [pscustomobject]@{ normalAuthorizationStarts = $normalResults; unsafeReturnTo = $unsafeResults }
}
finally {
    if (Test-Path -LiteralPath $workDir) { Remove-Item -LiteralPath $workDir -Recurse -Force }
}
