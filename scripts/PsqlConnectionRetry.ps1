# Retry only a psql initial-connect failure: no SQL has reached the server.
function Invoke-IntegrationPsql {
    param([string]$Executable)
    $nativeArguments = @($args)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $errorFile = [IO.Path]::GetTempFileName()
        $previousErrorAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $nativeOutput = @(& $Executable @nativeArguments 2> $errorFile)
            $nativeExitCode = $LASTEXITCODE
            $nativeError = [IO.File]::ReadAllText($errorFile)
        } finally {
            $ErrorActionPreference = $previousErrorAction
            Remove-Item -LiteralPath $errorFile -ErrorAction SilentlyContinue
        }
        $global:LASTEXITCODE = $nativeExitCode
        $initialSocketFailure = $nativeExitCode -eq 2 -and
            $nativeError -match '(?s)psql: error: connection to server .*failed: Bad address.*10014'
        if ($initialSocketFailure -and $attempt -lt 3) {
            Write-Host "Retrying psql initial Windows socket connection 10014 ($attempt/3)."
            Start-Sleep -Milliseconds 250
            continue
        }
        if ($nativeError) { [Console]::Error.Write($nativeError) }
        $nativeOutput
        return
    }
}
