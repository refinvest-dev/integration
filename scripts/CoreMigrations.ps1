function Get-CoreIntegrationMigrations {
    param([Parameter(Mandatory)][string]$CorePath)
    $pattern = Join-Path $CorePath '*/adapter/persistence/src/main/resources/db/migration/V*__*.sql'
    $files = @(Get-ChildItem -Path $pattern -File | Sort-Object { [int]($_.Name -replace '^V([0-9]+)__.*$', '$1') })
    $versions = @{}
    foreach ($file in $files) {
        if ($file.Name -notmatch '^V([0-9]+)__[a-z0-9_]+\.sql$') { throw 'Invalid Core migration filename' }
        $version = [int]$Matches[1]
        if ($versions.ContainsKey($version)) { throw "Duplicate Core migration version: $version" }
        $versions[$version] = $true
    }
    if ($files.Count -eq 0) { throw 'Core migration set is empty' }
    $files
}
