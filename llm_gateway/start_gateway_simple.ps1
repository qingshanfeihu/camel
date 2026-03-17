param(
    [Parameter(Mandatory = $true)]
    [string]$LogFile,
    [string]$PythonExe = "python"
)

# Resolve paths
$workingDirectory = (Resolve-Path ".").Path
$startScript = Join-Path $workingDirectory "llm_gateway/start.py"
if (-not (Test-Path $startScript)) {
    Write-Host "Error: start.py not found at $startScript"
    [Environment]::Exit(1)
}

Write-Host "Starting LLM Gateway..."
Write-Host "Working Directory: $workingDirectory"
Write-Host "Python: $PythonExe"
Write-Host "Script: $startScript"
Write-Host "Log File: $LogFile"
Write-Host ""
Write-Host "Press Ctrl+C to stop the service"
Write-Host "=" * 80
Write-Host ""

# Set environment for Python
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"

# Start Python process directly in foreground with output to both console and log
$continue = $true
while ($continue) {
    try {
        # Run python directly, letting output go to console naturally
        $process = Start-Process -FilePath $PythonExe -ArgumentList "-u","-X","utf8","`"$startScript`"" -WorkingDirectory $workingDirectory -NoNewWindow -Wait -PassThru
        
        $exitCode = $process.ExitCode
        
        if ($exitCode -ne 0) {
            Write-Host ""
            Write-Host "Gateway exited with code $exitCode. Restarting in 3 seconds..." -ForegroundColor Yellow
            Start-Sleep -Seconds 3
            continue
        }
        
        Write-Host "Gateway stopped normally."
        $continue = $false
    }
    catch {
        Write-Host "Error starting gateway: $_" -ForegroundColor Red
        [Environment]::Exit(1)
    }
}

[Environment]::Exit(0)
