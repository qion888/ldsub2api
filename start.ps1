$ErrorActionPreference = 'Stop'

$backendDirectory = Join-Path $PSScriptRoot 'backend'
$frontendDirectory = Join-Path $PSScriptRoot 'frontend'
$nodeModules = Join-Path $frontendDirectory 'node_modules'
$viteCommand = Join-Path $nodeModules '.bin\vite.cmd'
$requirements = Join-Path $backendDirectory 'requirements.txt'
$runtimeDirectory = Join-Path $PSScriptRoot '.runtime'
$backendPidFile = Join-Path $runtimeDirectory 'backend.pid'

function Resolve-LdxpPython {
    $candidates = @()
    $fallback = $null
    if (-not [string]::IsNullOrWhiteSpace($env:LDXP_PYTHON)) {
        $candidates += $env:LDXP_PYTHON
    }
    $candidates += @(
        (Join-Path $runtimeDirectory 'python\Scripts\python.exe'),
        (Join-Path $runtimeDirectory 'python\python.exe'),
        (Join-Path $runtimeDirectory 'python.exe')
    )
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -0p 2>$null | ForEach-Object {
            if ([string]$_ -match '([A-Za-z]:\\.*\\python(?:[0-9.]*)?\.exe)\s*$') {
                $candidates += $Matches[1]
            }
        }
    }
    $candidates += 'python'
    $seen = @{}
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace([string]$candidate)) { continue }
        $candidateKey = ([string]$candidate).ToLowerInvariant()
        if ($seen.ContainsKey($candidateKey)) { continue }
        $seen[$candidateKey] = $true
        $resolved = $null
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
        }
        else {
            $command = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($command) {
                $resolved = $command.Source
            }
        }
        if ($resolved) {
            if (-not $fallback) {
                $fallback = $resolved
            }
            & $resolved -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $resolved
            }
        }
    }
    if ($fallback) {
        Write-Warning 'Python 3.10-3.13 was not found. The app will start with the available interpreter and use manual captcha entry if OCR cannot load.'
        return $fallback
    }
    throw 'Python interpreter not found. Set LDXP_PYTHON or install Python.'
}

function Test-LdxpBackendDependencies([string]$PythonExecutable) {
    & $PythonExecutable -c "import selenium, ddddocr, numpy, onnxruntime" *> $null
    return $LASTEXITCODE -eq 0
}

function Stop-RecordedLdxpBackend {
    if (-not (Test-Path -LiteralPath $backendPidFile)) { return }
    $recordedPid = 0
    $recordedValue = [string](Get-Content -LiteralPath $backendPidFile -Raw)
    if ([int]::TryParse($recordedValue.Trim(), [ref]$recordedPid)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction SilentlyContinue
        $backendScript = (Join-Path $PSScriptRoot 'backend\main.py').ToLowerInvariant()
        if ($process -and ([string]$process.CommandLine).ToLowerInvariant().Contains($backendScript)) {
            Stop-Process -Id $recordedPid -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $backendPidFile -Force -ErrorAction SilentlyContinue
}

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

function Wait-LdxpBackend([int]$Port, $Process) {
    foreach ($attempt in 1..50) {
        if ($Process.HasExited) {
            throw "Backend exited before becoming ready (exit code $($Process.ExitCode))"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.ok -and $health.sub2api_accounts) {
                return
            }
        }
        catch {}
        Start-Sleep -Milliseconds 200
    }
    throw 'Backend readiness check failed: Sub2API account-list route was not loaded'
}

if (-not (Test-Path -LiteralPath $viteCommand)) {
    Write-Host 'Installing frontend dependencies...'
    npm --prefix $frontendDirectory install
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$pythonExecutable = Resolve-LdxpPython
Stop-RecordedLdxpBackend
Stop-StaleLdxpProcesses

if (Test-Path $requirements) {
    $backendDependenciesReady = Test-LdxpBackendDependencies $pythonExecutable
    if (-not $backendDependenciesReady) {
        Write-Host 'Installing backend browser verification and OCR dependencies...'
        & $pythonExecutable -m pip install -r $requirements
        $backendDependenciesReady = $LASTEXITCODE -eq 0 -and (Test-LdxpBackendDependencies $pythonExecutable)
        if (-not $backendDependenciesReady) {
            $runtimeEnvironment = Join-Path $runtimeDirectory 'python'
            $runtimePython = Join-Path $runtimeEnvironment 'Scripts\python.exe'
            $selectedPython = [IO.Path]::GetFullPath($pythonExecutable)
            $runtimePythonPath = [IO.Path]::GetFullPath($runtimePython)
            if (-not $selectedPython.Equals($runtimePythonPath, [StringComparison]::OrdinalIgnoreCase)) {
                Write-Host 'Creating an isolated backend Python runtime...'
                & $pythonExecutable -m venv $runtimeEnvironment
                if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
                    & $runtimePython -m pip install -r $requirements
                    $backendDependenciesReady = $LASTEXITCODE -eq 0 -and (Test-LdxpBackendDependencies $runtimePython)
                    if ($backendDependenciesReady) {
                        $pythonExecutable = $runtimePython
                    }
                }
            }
        }
        if (-not $backendDependenciesReady) {
            Write-Warning 'Some optional backend dependencies could not be installed. Order lookup will offer manual captcha entry when OCR is unavailable.'
        }
    }
}

$backendPort = Get-FreeLocalPort 8000
$frontendPort = Get-FreeLocalPort 5173
$env:LDXP_PORT = [string]$backendPort
$env:LDXP_FRONTEND_URL = "http://127.0.0.1:$frontendPort/"

Write-Host "Starting backend at http://127.0.0.1:$backendPort"
$backendScript = Join-Path $backendDirectory 'main.py'
$backendProcess = Start-Process $pythonExecutable `
    -ArgumentList ('"{0}"' -f $backendScript) `
    -WorkingDirectory $backendDirectory `
    -WindowStyle Hidden `
    -PassThru

try {
    Set-Content -LiteralPath $backendPidFile -Value $backendProcess.Id -NoNewline
    Wait-LdxpBackend $backendPort $backendProcess
    $env:LDXP_API_TARGET = "http://127.0.0.1:$backendPort"
    Write-Host ''
    Write-Host '========================================' -ForegroundColor DarkGray
    Write-Host 'LDXP services started' -ForegroundColor Green
    Write-Host "Frontend:      http://127.0.0.1:$frontendPort" -ForegroundColor Cyan
    Write-Host "Backend API:   http://127.0.0.1:$backendPort" -ForegroundColor Cyan
    Write-Host "Health check:  http://127.0.0.1:$backendPort/api/health" -ForegroundColor Cyan
    Write-Host "Account list:  http://127.0.0.1:$backendPort/api/sub2api/accounts" -ForegroundColor Cyan
    Write-Host "Ports:         frontend=$frontendPort  backend=$backendPort" -ForegroundColor Yellow
    Write-Host 'Press Ctrl+C to stop both services.' -ForegroundColor DarkGray
    Write-Host '========================================' -ForegroundColor DarkGray
    npm --prefix $frontendDirectory run dev -- --host 127.0.0.1 --port $frontendPort --strictPort
}
finally {
    if (-not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id
    }
    Remove-Item -LiteralPath $backendPidFile -Force -ErrorAction SilentlyContinue
}
