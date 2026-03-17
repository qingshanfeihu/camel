param(
    [Parameter(Mandatory = $true)]
    [string]$LogFile,
    [string]$PythonExe = "python"
)

$continue = $true
while ($continue) {
    $stoppedByUser = $false
    Write-Host "Starting LLM Gateway using $PythonExe ..."

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonExe
    # Resolve start.py path explicitly to avoid relative path issues
    $workingDirectory = (Resolve-Path ".").Path
    $startScript = Join-Path $workingDirectory "llm_gateway/start.py"
    if (-not (Test-Path $startScript)) {
        Write-Host "Error: start.py not found at $startScript"
        [Environment]::Exit(1)
    }

    $psi.Arguments = "-u -X utf8 `"$startScript`""
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $false  # Show window to help debug
    
    # Ensure working directory is set correctly (process inherits, but explicit is safer)
    $psi.WorkingDirectory = $workingDirectory

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $logWriter = [System.IO.StreamWriter]::new(
        $LogFile,
        $true,
        [System.Text.UTF8Encoding]::new($false)
    )

    # Use synchronous reading with separate threads
    $outJob = $null
    $errJob = $null
    
    $outputHandler = {
        param($proc, $logWriter)
        try {
            $reader = $proc.StandardOutput
            while (-not $reader.EndOfStream) {
                $line = $reader.ReadLine()
                if ($line) {
                    $logWriter.WriteLine($line)
                    $logWriter.Flush()
                    Write-Host $line
                }
            }
        } catch {}
    }
    
    $errorHandler = {
        param($proc, $logWriter)
        try {
            $reader = $proc.StandardError
            while (-not $reader.EndOfStream) {
                $line = $reader.ReadLine()
                if ($line) {
                    $logWriter.WriteLine("[STDERR] $line")
                    $logWriter.Flush()
                    Write-Host "[STDERR] $line" -ForegroundColor Red
                }
            }
        } catch {}
    }

    # Write diagnostics to log
    $logWriter.WriteLine("[Gateway Runner] WorkingDirectory: $workingDirectory")
    $logWriter.WriteLine("[Gateway Runner] PythonExe: $PythonExe")
    $logWriter.WriteLine("[Gateway Runner] StartScript: $startScript")
    $logWriter.WriteLine("[Gateway Runner] Arguments: $($psi.Arguments)")
    $logWriter.Flush()

    try {
        $null = $proc.Start()
    }
    catch {
        Write-Host "Error starting python process: $_"
        $logWriter.WriteLine("[Gateway Runner] Error starting process: $_")
        $logWriter.Close()
        exit 1
    }

    # Start output reading jobs
    $outJob = Start-Job -ScriptBlock $outputHandler -ArgumentList $proc, $logWriter
    $errJob = Start-Job -ScriptBlock $errorHandler -ArgumentList $proc, $logWriter

    Write-Host "Press Enter to stop the service (logs will continue in this window)."

    while (-not $proc.HasExited) {
        if ($Host.UI.RawUI.KeyAvailable) {
            $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyUp")
            # Check for Enter key (13) and ensure it's the KeyDown event
            if ($key.VirtualKeyCode -eq 13 -and $key.KeyDown) {
                $stoppedByUser = $true
                Write-Host "Stopping gateway..."
                try { $proc.Kill() } catch {}
                break
            }
        }
        Start-Sleep -Milliseconds 200
    }

    # Wait for process and jobs to complete
    $proc.WaitForExit()
    if ($outJob) { Wait-Job $outJob -Timeout 2 | Out-Null; Remove-Job $outJob -Force }
    if ($errJob) { Wait-Job $errJob -Timeout 2 | Out-Null; Remove-Job $errJob -Force }

    $exitCode = $proc.ExitCode
    $logWriter.WriteLine("[Gateway Runner] Process exited with code: $exitCode")
    $logWriter.Close()

    if ($stoppedByUser) {
        Write-Host "Gateway stopped by user."
        [Environment]::Exit(0)
    }

    if ($exitCode -ne 0) {
        Write-Host ("Gateway exited with code " + $exitCode + ". Restarting in 3 seconds...")
        Start-Sleep -Seconds 3
        continue
    }

    Write-Host "Gateway stopped normally."
    [Environment]::Exit(0)
}
