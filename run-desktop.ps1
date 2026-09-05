$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'LegiView - Local Server'

$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$appUrl = 'http://127.0.0.1:5055'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host 'LegiView virtual environment was not found.' -ForegroundColor Red
    Write-Host 'Run these commands from C:\Projects\LegiView:'
    Write-Host '  py -m venv .venv'
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -e .'
    return
}

function Test-LegiViewReady {
    try {
        $health = Invoke-RestMethod -Uri "$appUrl/health" -TimeoutSec 2
        return ($health.status -eq 'ok' -and $null -ne $health.schema_version -and $null -ne $health.workers)
    }
    catch {
        return $false
    }
}

if (Test-LegiViewReady) {
    Write-Host "LegiView is already running at $appUrl" -ForegroundColor Green
    Start-Process $appUrl
    return
}

Write-Host "Starting LegiView at $appUrl ..." -ForegroundColor Cyan
Write-Host 'Keep this PowerShell window open while using LegiView. Press Ctrl+C to stop the server.'
$appProcess = Start-Process -FilePath $pythonPath `
    -ArgumentList '-m', 'olis_archive', 'serve', '--host', '127.0.0.1', '--port', '5055' `
    -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru

$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if ($appProcess.HasExited) {
        Write-Host 'LegiView exited during startup. See the error above.' -ForegroundColor Red
        return
    }
    if (Test-LegiViewReady) {
        $ready = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if ($ready) {
    Write-Host "LegiView is ready. Opening $appUrl" -ForegroundColor Green
    Start-Process $appUrl
}
else {
    Write-Host "Startup is taking longer than expected. Open $appUrl when the server is ready." -ForegroundColor Yellow
}

Wait-Process -Id $appProcess.Id -ErrorAction SilentlyContinue
