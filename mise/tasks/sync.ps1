#MISE description="Framework の Python 依存関係を同期する"

$ErrorActionPreference = "Stop"
$rootDir = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$exitCode = 0

Push-Location $rootDir
try {
    & uv sync
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
