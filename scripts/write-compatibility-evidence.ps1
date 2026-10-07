[CmdletBinding()]
param(
    [string]$ContextPath = (Join-Path $PSScriptRoot '..\..\context'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web'),
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\.integration-data\evidence\compatibility.json'),
    [string]$VerificationName = 'not-recorded',
    [ValidateSet('success', 'failure', 'cancelled', 'skipped', 'not-recorded')]
    [string]$VerificationOutcome = 'not-recorded',
    [ValidateSet('context', 'coreApi', 'computeApi', 'web', 'integration')]
    [string[]]$RequiredRepositories = @('context', 'coreApi', 'computeApi', 'integration')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$integrationPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Get-PortableRelativePath {
    param(
        [Parameter(Mandatory)] [string]$BasePath,
        [Parameter(Mandatory)] [string]$TargetPath,
        [switch]$TargetIsDirectory
    )

    $baseFullPath = [IO.Path]::GetFullPath($BasePath).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $targetFullPath = [IO.Path]::GetFullPath($TargetPath)
    $relativePathMethod = [IO.Path].GetMethods() |
        Where-Object { $_.Name -ceq 'GetRelativePath' -and $_.GetParameters().Count -eq 2 } |
        Select-Object -First 1
    if ($null -ne $relativePathMethod) {
        return ([string]$relativePathMethod.Invoke($null, @($BasePath, $targetFullPath))).Replace('\', '/')
    }

    # Windows PowerShell 5.1 runs on .NET Framework, which does not expose
    # Path.GetRelativePath. Its absolute drive paths are safe to compare as
    # file URIs; PowerShell 7+ takes the cross-platform branch above.
    $targetUriPath = if ($TargetIsDirectory) {
        $targetFullPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    }
    else {
        $targetFullPath
    }
    $baseUri = [Uri]$baseFullPath
    $targetUri = [Uri]$targetUriPath
    if ($baseUri.Scheme -cne $targetUri.Scheme) {
        return $targetFullPath.Replace('\', '/')
    }

    $relativePath = [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).TrimEnd('/')
    return $(if ([string]::IsNullOrWhiteSpace($relativePath)) { '.' } else { $relativePath })
}

function Get-RepositoryEvidence {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$Path
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $checkoutPath = Get-PortableRelativePath -BasePath $integrationPath -TargetPath $fullPath -TargetIsDirectory
    $entry = [ordered]@{
        name = $Name
        checkoutPath = $checkoutPath
        available = $false
        revision = $null
        branch = $null
        worktree = 'missing'
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $entry
    }

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $insideWorktree = & git -C $resolvedPath rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $insideWorktree -cne 'true') {
        $entry.worktree = 'not-a-git-worktree'
        return $entry
    }

    $revision = & git -C $resolvedPath rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($revision)) {
        throw "Unable to resolve HEAD for $Name at $resolvedPath."
    }

    $branch = & git -C $resolvedPath branch --show-current
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve the branch for $Name at $resolvedPath."
    }

    $status = @(& git -C $resolvedPath status --porcelain=v1 --untracked-files=normal)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the worktree for $Name at $resolvedPath."
    }

    $entry.available = $true
    $entry.revision = $revision.Trim()
    $entry.branch = if ([string]::IsNullOrWhiteSpace($branch)) { $null } else { $branch.Trim() }
    $entry.worktree = if ($status.Count -eq 0) { 'clean' } else { 'dirty' }
    return $entry
}

function Get-NormalizedTextSha256 {
    param([Parameter(Mandatory)] [string]$Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $contractPath = Get-PortableRelativePath -BasePath $integrationPath -TargetPath $fullPath
    $entry = [ordered]@{
        path = $contractPath
        available = $false
        sha256 = $null
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $entry
    }

    $text = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path).Path).Replace("`r`n", "`n")
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))
    }
    finally {
        $sha256.Dispose()
    }

    $entry.available = $true
    $entry.sha256 = [BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
    return $entry
}

$repositories = [ordered]@{
    context = Get-RepositoryEvidence -Name 'context' -Path $ContextPath
    coreApi = Get-RepositoryEvidence -Name 'core-api' -Path $CoreApiPath
    computeApi = Get-RepositoryEvidence -Name 'compute-api' -Path $ComputeApiPath
    web = Get-RepositoryEvidence -Name 'web' -Path $WebPath
    integration = Get-RepositoryEvidence -Name 'integration' -Path $integrationPath
}

$contracts = [ordered]@{
    contextCoreApi = Get-NormalizedTextSha256 (Join-Path $ContextPath 'openapi\core-api.yaml')
    coreCoreApi = Get-NormalizedTextSha256 (Join-Path $CoreApiPath 'openapi\core-api.yaml')
    contextComputeApi = Get-NormalizedTextSha256 (Join-Path $ContextPath 'openapi\compute-api.yaml')
    coreComputeApi = Get-NormalizedTextSha256 (Join-Path $CoreApiPath 'openapi\compute-api.yaml')
    computeComputeApi = Get-NormalizedTextSha256 (Join-Path $ComputeApiPath 'openapi\compute-api.yaml')
    webCoreApi = Get-NormalizedTextSha256 (Join-Path $WebPath 'docs\api\open-api.yaml')
    webComputeApi = Get-NormalizedTextSha256 (Join-Path $WebPath 'docs\api\compute-api.yaml')
}

$validationErrors = @()
if ($VerificationOutcome -ceq 'success') {
    foreach ($repositoryName in $RequiredRepositories) {
        if (-not $repositories[$repositoryName].available) {
            $validationErrors += "Required repository is unavailable: $repositoryName"
        }
    }

    $contractComparisons = @(
        @('contextCoreApi', 'coreCoreApi'),
        @('contextComputeApi', 'coreComputeApi'),
        @('contextComputeApi', 'computeComputeApi')
    )
    if ($RequiredRepositories -contains 'web') {
        $contractComparisons += ,@('contextCoreApi', 'webCoreApi')
        $contractComparisons += ,@('contextComputeApi', 'webComputeApi')
    }

    foreach ($comparison in $contractComparisons) {
        $source = $contracts[$comparison[0]]
        $replica = $contracts[$comparison[1]]
        if (-not $source.available -or -not $replica.available) {
            $validationErrors += "Contract evidence is unavailable: $($comparison -join ' vs ')"
        }
        elseif ($source.sha256 -cne $replica.sha256) {
            $validationErrors += "Contract evidence differs: $($comparison -join ' vs ')"
        }
    }
}

$evidence = [ordered]@{
    schemaVersion = 1
    generatedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    verification = [ordered]@{
        name = $VerificationName
        outcome = $VerificationOutcome
    }
    validation = [ordered]@{
        outcome = if ($validationErrors.Count -eq 0) { 'success' } else { 'failure' }
        errors = $validationErrors
    }
    repositories = $repositories
    contracts = $contracts
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$json = $evidence | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($resolvedOutput, "$json`n", [Text.UTF8Encoding]::new($false))

Write-Host "Compatibility evidence written to $resolvedOutput"
if ($validationErrors.Count -ne 0) {
    throw "Compatibility evidence validation failed: $($validationErrors -join '; ')"
}
