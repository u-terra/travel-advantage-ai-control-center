$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

Write-Host "==> py_compile: app/ tests/"
$pyFiles = Get-ChildItem -Path "app", "tests" -Recurse -Filter "*.py" |
    ForEach-Object { $_.FullName }
if ($pyFiles.Count -eq 0) {
    Write-Host "Нет файлов .py для проверки."
} else {
    & $python -m py_compile @pyFiles
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "==> pytest: tests/"
& $python -m pytest -q tests
exit $LASTEXITCODE
