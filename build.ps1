$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment not found at $python"
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name "LPUScheduler" `
    --collect-all playwright `
    (Join-Path $PSScriptRoot "app.py")

Write-Host ""
Write-Host "Executable created at: $(Join-Path $PSScriptRoot 'dist\LPUScheduler.exe')"
Write-Host "Copy credentials.json beside the executable before running it."
