Set-StrictMode -Version Latest

function Get-IntegrationRepositoryIdentity {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
    $revision = (& git -C $resolved rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($revision)) {
        throw "Cannot resolve Git revision for '$resolved'."
    }
    $dirty = -not [string]::IsNullOrWhiteSpace((& git -C $resolved status --porcelain 2>$null) -join "`n")
    [pscustomobject]@{ path = $resolved; revision = $revision; dirty = $dirty }
}

function ConvertTo-IntegrationCanonicalValue {
    param($Value)

    if ($null -eq $Value) { return $null }
    if ($Value -is [string] -or $Value.GetType().IsValueType) { return $Value }
    if ($Value -is [System.Collections.IDictionary]) {
        $ordered = [ordered]@{}
        foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $ordered[$key] = ConvertTo-IntegrationCanonicalValue $Value[$key]
        }
        return $ordered
    }
    if ($Value -is [pscustomobject]) {
        $ordered = [ordered]@{}
        foreach ($property in @($Value.PSObject.Properties | Sort-Object Name)) {
            $ordered[$property.Name] = ConvertTo-IntegrationCanonicalValue $property.Value
        }
        return $ordered
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-IntegrationCanonicalValue $_ })
    }
    return $Value
}

function Get-IntegrationIdentityFingerprint {
    param([Parameter(Mandatory)]$Value)

    $json = (ConvertTo-IntegrationCanonicalValue $Value) | ConvertTo-Json -Depth 12 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function New-IntegrationServiceIdentity {
    param(
        [Parameter(Mandatory)][string]$ServiceRole,
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$RegistryId,
        [Parameter(Mandatory)][string]$RepositoryPath,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Configuration
    )

    if ($Profile -notmatch '^[a-z0-9][a-z0-9-]*$') { throw "Invalid integration profile '$Profile'." }
    if ($RegistryId -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw "Invalid integration registry ID '$RegistryId'." }
    $repository = Get-IntegrationRepositoryIdentity $RepositoryPath
    $normalizedDictionary = ConvertTo-IntegrationCanonicalValue $Configuration
    $normalized = [pscustomobject]$normalizedDictionary
    $fingerprintInput = [ordered]@{
        identityVersion = 1; serviceRole = $ServiceRole; profile = $Profile; registryId = $RegistryId
        repositoryPath = $repository.path; repositoryRevision = $repository.revision; configuration = $normalizedDictionary
    }
    [pscustomobject][ordered]@{
        identityVersion = 1; serviceRole = $ServiceRole; profile = $Profile; registryId = $RegistryId
        repositoryPath = $repository.path; repositoryRevision = $repository.revision
        repositoryDirty = $repository.dirty; configuration = $normalized
        configurationFingerprint = Get-IntegrationIdentityFingerprint $fingerprintInput
    }
}

function Get-IntegrationIdentityDifferences {
    param([Parameter(Mandatory)]$Expected, [Parameter(Mandatory)]$Actual)

    $differences = [Collections.Generic.List[string]]::new()
    foreach ($name in @('identityVersion','serviceRole','profile','registryId','repositoryPath','repositoryRevision','configurationFingerprint')) {
        $expectedValue = $Expected.PSObject.Properties[$name].Value
        $actualProperty = $Actual.PSObject.Properties[$name]
        $actualValue = if ($null -eq $actualProperty) { '<missing>' } else { $actualProperty.Value }
        if ([string]$expectedValue -cne [string]$actualValue) { $differences.Add("$name expected='$expectedValue' actual='$actualValue'") }
    }
    $actualConfiguration = $Actual.PSObject.Properties['configuration']
    if ($null -ne $Expected.configuration -and $null -ne $actualConfiguration) {
        foreach ($property in $Expected.configuration.PSObject.Properties) {
            $actualProperty = $actualConfiguration.Value.PSObject.Properties[$property.Name]
            $actualValue = if ($null -eq $actualProperty) { '<missing>' } else { $actualProperty.Value }
            if ([string]$property.Value -cne [string]$actualValue) {
                $differences.Add("configuration.$($property.Name) expected='$($property.Value)' actual='$actualValue'")
            }
        }
    }
    @($differences)
}

Export-ModuleMember -Function New-IntegrationServiceIdentity, Get-IntegrationIdentityDifferences, Get-IntegrationIdentityFingerprint
