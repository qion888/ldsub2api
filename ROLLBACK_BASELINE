$ErrorActionPreference = 'Stop'

$backendDirectory = Join-Path $PSScriptRoot 'backend'
$frontendDirectory = Join-Path $PSScriptRoot 'frontend'
$nodeModules = Join-Path $frontendDirectory 'node_modules'
$requirements = Join-Path $backendDirectory 'requirements.txt'

function Stop-StaleLdxpProcesses {
    $root = $PSScriptRoot.ToLowerInvariant()
    $backendScript = (Join-Path $root 'backend\main.py').ToLowerInvariant()
    Get-CimInstance Win32_Process | ForEach-Object {
        $commandLine = [string]$_.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) { return }
        $normalized = $commandLine.ToLowerInvariant()
        $isBackend = $normalized.Contains($backendScript)
        $isFrontend = $normalized.Contains((Join-Path $root 'frontend\node_modules').ToLowerInvariant()) -and $normalized.Contains('vite')
        if (($isBackend -or $isFrontend) -and $_.ProcessId -ne $PID) {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-FreeLocalPort([int]$StartPort) {
    foreach ($port in $StartPort..($StartPort + 50)) {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
        try {
            $listener.Start()
            return $port
        }
        catch {
            continue
        }
        finally {
            $listener.Stop()
        }
    }
    throw "No available local port found after $StartPort"
}

if (-not (Test-Path $nodeModules)) {
    Write-Host 'Installing frontend dependencies...'
    npm --prefix $frontendDirectory install
}

Stop-StaleLdxpProcesses

if (Test-Path $requirements) {
    python -c "import selenium" 2>$null
    $seleniumMissing = $LASTEXITCODE -ne 0
    if ($seleniumMissing) {
        Write-Host 'Installing backend browser verification dependency...'
        python -m pip install -r $requirements
    }
}

$backendPort = Get-FreeLocalPort 8000
$frontendPort = Get-FreeLocalPort 5173
$env:LDXP_PORT = [string]$backendPort
$env:LDXP_FRONTEND_URL = "http://127.0.0.1:$frontendPort/"

Write-Host "Starting backend at http://127.0.0.1:$backendPort"
$backendScript = Join-Path $backendDirectory 'main.py'
$backendProcess = Start-Process python `
    -ArgumentList ('"{0}"' -f $backendScript) `
    -WorkingDirectory $backendDirectory `
    -WindowStyle Hidden `
    -PassThru

try {
    $env:LDXP_API_TARGET = "http://127.0.0.1:$backendPort"
    Write-Host ''
    Write-Host '========================================' -ForegroundColor DarkGray
    Write-Host 'LDXP services started' -ForegroundColor Green
    Write-Host "Frontend:      http://127.0.0.1:$frontendPort" -ForegroundColor Cyan
    Write-Host "Backend API:   http://127.0.0.1:$backendPort" -ForegroundColor Cyan
    Write-Host "Health check:  http://127.0.0.1:$backendPort/api/health" -ForegroundColor Cyan
    Write-Host "Ports:         frontend=$frontendPort  backend=$backendPort" -ForegroundColor Yellow
    Write-Host 'Press Ctrl+C to stop both services.' -ForegroundColor DarkGray
    Write-Host '========================================' -ForegroundColor DarkGray
    npm --prefix $frontendDirectory run dev -- --host 127.0.0.1 --port $frontendPort --strictPort
}
finally {
    if (-not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id
    }
}
