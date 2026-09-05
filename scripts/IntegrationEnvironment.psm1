function Get-IntegrationEnvironment {
    param([Parameter(Mandatory)][string]$EnvironmentFile)

    if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
        throw "Integration environment file does not exist: $EnvironmentFile"
    }

    $values = @{}
    Get-Content -LiteralPath $EnvironmentFile | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        $values[$name.Trim()] = $value.Trim().Trim('"')
    }
    return $values
}

function Assert-IntegrationEnvironment {
    param([hashtable]$Values, [string[]]$Required)

    foreach ($name in $Required) {
        if (-not $Values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($Values[$name])) {
            throw "$name must be set in the integration environment file."
        }
    }
}

function Get-IntegrationPostgresHost {
    param([hashtable]$Values)
    if ($Values.ContainsKey('INTEGRATION_POSTGRES_HOST') -and -not [string]::IsNullOrWhiteSpace($Values['INTEGRATION_POSTGRES_HOST'])) {
        return $Values['INTEGRATION_POSTGRES_HOST']
    }
    return 'localhost'
}

function Get-IntegrationPsql {
    $candidate = @(
        (Get-Command psql.exe -ErrorAction SilentlyContinue).Source,
        'C:\Program Files\PostgreSQL\17\bin\psql.exe'
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    return $candidate
}

function Set-CoreIntegrationEnvironment {
    param([hashtable]$Values)
    Assert-IntegrationEnvironment $Values @('INTEGRATION_POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET', 'REFINVEST_INTERNAL_API_KEY')

    $env:DATABASE_HOST = Get-IntegrationPostgresHost $Values
    $env:DATABASE_PORT = $Values['INTEGRATION_POSTGRES_PORT']
    $env:DATABASE_NAME = $Values['POSTGRES_DB']
    $env:DATABASE_USERNAME = $Values['POSTGRES_USER']
    $env:DATABASE_PASSWORD = $Values['POSTGRES_PASSWORD']
    $env:COMPUTE_API_KEY = $Values['REFINVEST_INTERNAL_API_KEY']
    $env:COMPUTE_API_BASE_URL = if ($Values['COMPUTE_API_BASE_URL']) { $Values['COMPUTE_API_BASE_URL'] } else { "http://127.0.0.1:$($Values['INTEGRATION_COMPUTE_PORT'])" }
    foreach ($name in 'REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET', 'REFINVEST_WEB_ORIGIN', 'REFINVEST_COOKIE_SECURE', 'REFINVEST_COOKIE_SAME_SITE', 'REFINVEST_COMPUTE_ENABLED') {
        if ($Values.ContainsKey($name)) { Set-Item "Env:$name" $Values[$name] }
    }

    $oauthEnabled = $Values['REFINVEST_OAUTH_ENABLED'] -eq 'true'
    if (-not $oauthEnabled) { return }

    Assert-IntegrationEnvironment $Values @(
        'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
        'KAKAO_CLIENT_ID', 'KAKAO_CLIENT_SECRET',
        'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'
    )
    $env:SPRING_PROFILES_ACTIVE = 'oauth'
    foreach ($name in 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'KAKAO_CLIENT_ID', 'KAKAO_CLIENT_SECRET', 'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET') {
        Set-Item "Env:$name" $Values[$name]
    }
}

function Set-ComputeIntegrationEnvironment {
    param([hashtable]$Values, [string]$WorkspacePath)
    Assert-IntegrationEnvironment $Values @('INTEGRATION_POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'REFINVEST_INTERNAL_API_KEY', 'REFINVEST_SNAPSHOT_STORAGE_ROOT')

    $user = [uri]::EscapeDataString($Values['POSTGRES_USER'])
    $password = [uri]::EscapeDataString($Values['POSTGRES_PASSWORD'])
    $env:REFINVEST_DATABASE_URL = "postgresql://${user}:${password}@$(Get-IntegrationPostgresHost $Values):$($Values['INTEGRATION_POSTGRES_PORT'])/$($Values['POSTGRES_DB'])"
    $env:REFINVEST_INTERNAL_API_KEY = $Values['REFINVEST_INTERNAL_API_KEY']
    $env:REFINVEST_SNAPSHOT_STORAGE_BACKEND = 'filesystem'
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = [IO.Path]::GetFullPath((Join-Path $WorkspacePath $Values['REFINVEST_SNAPSHOT_STORAGE_ROOT']))
}

Export-ModuleMember -Function Get-IntegrationEnvironment, Assert-IntegrationEnvironment, Get-IntegrationPostgresHost, Get-IntegrationPsql, Set-CoreIntegrationEnvironment, Set-ComputeIntegrationEnvironment
