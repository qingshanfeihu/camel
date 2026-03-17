param(
    [ValidateSet("menu","db","init","interactive","e2e","jobs","web","review")]
    [string]$Mode = "menu",

    [ValidateSet("status","create","update","rebuild","delete")]
    [string]$DbAction = "status",

    [switch]$SkipPreInit
)

Push-Location (Split-Path $PSScriptRoot -Parent)

# Detect Python interpreter — prefer Python 3.10 (required by graphrag <3.13)
$script:PythonExe = "python"
$_py310 = "C:\Users\jiang\AppData\Local\Programs\Python\Python310\python.exe"
$_venvPy = Join-Path (Get-Location).Path ".venv\Scripts\python.exe"
if (Test-Path $_py310) {
    $script:PythonExe = $_py310
} elseif (Test-Path $_venvPy) {
    $script:PythonExe = $_venvPy
}

# ── UTF-8 Encoding Setup (fixes Chinese garbled output) ──────────────────────
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
# Force PowerShell to read child process stdout as UTF-8
if ($PSVersionTable.PSVersion.Major -le 5) {
    chcp 65001 | Out-Null
}

$LogDir = Join-Path $PSScriptRoot "knowledge_base\logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$LogFile = Join-Path $LogDir ("pipeline_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("INFO","WARN","ERROR","SUCCESS")]
        [string]$Level = "INFO"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[{0}] [{1}] {2}" -f $timestamp, $Level, $Message

    switch ($Level) {
        "ERROR"   { Write-Host $Message -ForegroundColor Red }
        "WARN"    { Write-Host $Message -ForegroundColor Yellow }
        "SUCCESS" { Write-Host $Message -ForegroundColor Green }
        default   { Write-Host $Message -ForegroundColor Cyan }
    }

    try {
        Add-Content -Path $LogFile -Value $logMessage -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {
        # ignore logging errors
    }
}

# Helper: run a Python script with proper UTF-8 piping to both console and log.
# Reads stdout/stderr line-by-line in real time (no buffering until exit).
function Invoke-PythonWithLog {
    param(
        [Parameter(Mandatory)]
        [string]$ScriptArgs
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PythonExe
    $psi.Arguments = "-u $ScriptArgs"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8
    $psi.WorkingDirectory = (Get-Location).Path

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    # Collect stderr asynchronously (usually small) while we read stdout line-by-line
    $stderrBuilder = New-Object System.Text.StringBuilder
    $stderrHandler = {
        if (-not [string]::IsNullOrEmpty($EventArgs.Data)) {
            $stderrBuilder.AppendLine($EventArgs.Data) | Out-Null
        }
    }
    $proc.EnableRaisingEvents = $true
    Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $stderrHandler -MessageData $stderrBuilder | Out-Null

    $proc.Start() | Out-Null
    $proc.BeginErrorReadLine()

    # Read stdout line-by-line in real time
    while ($null -ne ($line = $proc.StandardOutput.ReadLine())) {
        Write-Host $line
        try {
            Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
        } catch {}
    }

    $proc.WaitForExit()

    # Flush any remaining stderr
    $stderrText = $stderrBuilder.ToString()
    if ($stderrText.Trim()) {
        foreach ($errLine in ($stderrText -split "`r?`n")) {
            if ($errLine -ne "") {
                Write-Host $errLine -ForegroundColor Yellow
                try {
                    Add-Content -Path $LogFile -Value "[STDERR] $errLine" -Encoding UTF8 -ErrorAction SilentlyContinue
                } catch {}
            }
        }
    }

    # Clean up event subscription
    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $proc } | Unregister-Event -ErrorAction SilentlyContinue

    return $proc.ExitCode
}

function Invoke-PreInitDatabase {
    Write-Log "==================================================================================" "INFO"
    Write-Log "Pre-init: check and initialize database if missing" "INFO"
    Write-Log "==================================================================================" "INFO"

    $kbPath    = Join-Path $PSScriptRoot "knowledge_base\reference\knowledge_base.json"
    $indexPath = Join-Path $PSScriptRoot "knowledge_base\function_structure_index.json"

    $kbExists    = Test-Path $kbPath
    $indexExists = Test-Path $indexPath

    if ($kbExists -and $indexExists) {
        Write-Log "Database exists. Skip initialization." "INFO"
        if ($kbExists) {
            Write-Log "  - knowledge_base.json: present" "INFO"
        } else {
            Write-Log "  - knowledge_base.json: missing" "INFO"
        }
        if ($indexExists) {
            Write-Log "  - function_structure_index.json: present" "INFO"
        } else {
            Write-Log "  - function_structure_index.json: missing" "INFO"
        }
        return $true
    }

    Write-Log "Database incomplete. Start initialization..." "WARN"
    if (-not $kbExists) {
        Write-Log "  - knowledge_base.json: missing, will be created." "WARN"
    }
    if (-not $indexExists) {
        Write-Log "  - function_structure_index.json: missing, will be created." "WARN"
    }

    Write-Log ""
    Write-Log "Running database initialization (may take some time)..." "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/manage_database.py --action create"

    if ($exitCode -ne 0) {
        Write-Log "Database initialization FAILED." "ERROR"
        return $false
    }

    Write-Log "Database initialization completed." "SUCCESS"
    Write-Log ""
    return $true
}

function Invoke-DbAction {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("status","create","update","rebuild","delete")]
        [string]$Action
    )

    Write-Log ""
    Write-Log ("Database action: {0}" -f $Action) "INFO"
    Write-Log ""

    switch ($Action) {
        "status" {
            Write-Log "Show database status and file changes in knowledge_base." "INFO"
        }
        "create" {
            Write-Log "Create database only when missing." "INFO"
        }
        "update" {
            Write-Log "Incremental update: detect new/changed files (PDF + Office) and auto-classify, update database and index." "INFO"
        }
        "rebuild" {
            Write-Log "Force full rebuild: clear existing database and re-process all source files (PDF + Office with auto-classification)." "INFO"
            $confirm = Read-Host "Are you sure you want to CLEAR all data and rebuild? (yes/no)"
            if ($confirm -ne "yes") {
                Write-Log "[CANCELLED] Rebuild aborted by user." "WARN"
                return
            }
        }
        "delete" {
            Write-Log "Delete database files and local RAG index (keep raw source files and MinerU outputs)." "INFO"
            $confirm = Read-Host "Are you sure you want to DELETE all database files? (yes/no)"
            if ($confirm -ne "yes") {
                Write-Log "[CANCELLED] Delete aborted by user." "WARN"
                return
            }
        }
    }

    $exitCode = Invoke-PythonWithLog "INAGENT/manage_database.py --action $Action"

    if ($exitCode -ne 0) {
        Write-Log ("[ERROR] Database action failed: {0}" -f $Action) "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ("[SUCCESS] Database action completed: {0}" -f $Action) "SUCCESS"

    # GraphRAG: sync or rebuild according to action
    $graphragWorkspace = Join-Path $PSScriptRoot "graphrag_index"
    $graphragOutput = Join-Path $graphragWorkspace "output"
    $kbPath = Join-Path $PSScriptRoot "knowledge_base\reference\knowledge_base.json"

    switch ($Action) {
        "status" {
            Write-Log "" "INFO"
            Write-Log "GraphRAG status:" "INFO"
            $null = Invoke-PythonWithLog "INAGENT/scripts/init_graphrag.py --status"
        }
        { $_ -in @("create", "update", "rebuild") } {
            # Unified GraphRAG build using LLM Gateway (non-batch)
            # Automatically: init workspace -> build index
            if (Test-Path $kbPath) {
                Write-Log "" "INFO"
                Write-Log "GraphRAG: Starting full build pipeline (LLM Gateway)..." "INFO"
                Write-Host ""
                Write-Host "Building GraphRAG index via LLM Gateway..." -ForegroundColor Green
                Write-Host "This will: init workspace -> build index" -ForegroundColor Cyan
                Write-Host "Build phases: extract_graph -> summarize -> embed -> communities -> reports" -ForegroundColor Cyan
                Write-Host "Progress bar will update every 15 seconds during long phases." -ForegroundColor Gray
                Write-Host ""
                
                $env:PYTHONIOENCODING = "utf-8"
                
                # Step 1: Init workspace
                Write-Log "Step 1/2: Initializing GraphRAG workspace..." "INFO"
                $initExitCode = Invoke-PythonWithLog "INAGENT/scripts/init_graphrag.py --init"
                
                if ($initExitCode -ne 0) {
                    Write-Log "[ERROR] GraphRAG workspace init failed." "ERROR"
                } else {
                    Write-Log "[SUCCESS] Step 1/2: Workspace initialized." "SUCCESS"
                    Write-Host ""
                    
                    # Step 2: Build index (with real-time progress monitor)
                    Write-Log "Step 2/2: Building GraphRAG index (this takes a while)..." "INFO"
                    $graphExitCode = Invoke-PythonWithLog "INAGENT/scripts/init_graphrag.py --build"
                    
                    if ($graphExitCode -eq 0) {
                        Write-Log "" "SUCCESS"
                        Write-Log "[SUCCESS] GraphRAG build completed. Ready for workflow use." "SUCCESS"
                    } else {
                        Write-Log "[ERROR] GraphRAG build failed. Check logs for details." "ERROR"
                    }
                }
            } else {
                Write-Log "[WARN] knowledge_base.json not found. Run database create first." "WARN"
            }
        }
        "delete" {
            if (Test-Path $graphragOutput) {
                Write-Log "" "INFO"
                Write-Log "GraphRAG: removing workspace output (index cleared)." "INFO"
                Remove-Item -Path $graphragOutput -Recurse -Force -ErrorAction SilentlyContinue
                if (Test-Path $graphragOutput) {
                    Write-Log "[WARN] Could not remove GraphRAG output; remove graphrag_index manually if needed." "WARN"
                } else {
                    Write-Log "[SUCCESS] GraphRAG output removed." "SUCCESS"
                }
            }

            $batchState = Join-Path $graphragWorkspace "batch_state.json"
            if (Test-Path $batchState) {
                Write-Log "GraphRAG: clearing batch state to avoid stale cache." "INFO"
                Remove-Item -Path $batchState -Force -ErrorAction SilentlyContinue
                if (Test-Path $batchState) {
                    Write-Log "[WARN] Could not remove batch_state.json; delete manually if needed." "WARN"
                } else {
                    Write-Log "[SUCCESS] Batch state removed." "SUCCESS"
                }
            }
        }
    }
}

function Invoke-InteractiveConfig {
    Write-Log ""
    Write-Log "Start interactive config generator (unified RAG)." "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/interactive_cli.py"

    if ($exitCode -ne 0) {
        Write-Log "[ERROR] Interactive config generation failed." "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ""
    Write-Log "[SUCCESS] Interactive config generation completed." "SUCCESS"
}

function Invoke-FullInit {
    Write-Log ""
    Write-Log "Run full initialization workflow (all base steps)." "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/initialize_pipeline.py"

    if ($exitCode -ne 0) {
        Write-Log "[ERROR] Full initialization failed." "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ""
    Write-Log "[SUCCESS] Full initialization completed." "SUCCESS"
}

function Invoke-E2EPipeline {
    Write-Log ""
    Write-Log "Run end-to-end pipeline (Stages 1-9)." "INFO"
    Write-Log "  Stage 1  : Test Plan Generation (RAG + Agent)" "INFO"
    Write-Log "  Stage 2  : Network Environment Planning" "INFO"
    Write-Log "  Stage 3  : Task Decomposition & Config Generation" "INFO"
    Write-Log "  Stage 4  : VM Test Environment Deployment" "INFO"
    Write-Log "  Stage 5-9: CAMEL Workforce Pipeline" "INFO"
    Write-Log "      5 - DeployWorker      (Config Push)" "INFO"
    Write-Log "      6 - TrafficWorker     (HTTP Verify + Fault Inject)" "INFO"
    Write-Log "      7 - VerifyShowWorker  (SSH Health Check)" "INFO"
    Write-Log "      8 - AnalysisWorker    (AI Verdict)" "INFO"
    Write-Log "      9 - CleanupWorker     (Environment Cleanup)" "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/pipeline_runner.py"

    if ($exitCode -ne 0) {
        Write-Log "[ERROR] E2E pipeline execution failed." "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ""
    Write-Log "[SUCCESS] E2E pipeline completed. Reports: INAGENT/reports/" "SUCCESS"
}

function Invoke-Jobs {
    Write-Log ""
    Write-Log "Run workflow_config_generator for all jobs (config generation only, no execution)." "INFO"
    Write-Log "  Input: INAGENT/jobs/config_tasks/" "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/workflow_config_generator.py"

    if ($exitCode -ne 0) {
        Write-Log "[ERROR] Jobs processing failed." "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ""
    Write-Log "[SUCCESS] Jobs processing completed. Reports: INAGENT/reports/" "SUCCESS"
}

function Invoke-TestReview {
    Write-Log ""
    Write-Log "Run test case review (ReviewPipeline: plan → knowledge → review)." "INFO"
    Write-Log "  Input : INAGENT/jobs/test_review/  (放入 .xlsx/.xls 文件)" "INFO"
    Write-Log "  Output: INAGENT/review_results/" "INFO"
    Write-Log ""

    # Check if there are Excel files to review
    $reviewDir = Join-Path $PSScriptRoot "jobs\test_review"
    if (-not (Test-Path $reviewDir)) {
        New-Item -ItemType Directory -Path $reviewDir -Force | Out-Null
    }
    $excelFiles = Get-ChildItem -Path $reviewDir -Include "*.xlsx","*.xls" -File -ErrorAction SilentlyContinue
    if (-not $excelFiles) {
        Write-Log "[WARN] INAGENT/jobs/test_review/ 目录下没有找到 Excel 文件。" "WARN"
        Write-Log "请将待评审的 .xlsx/.xls 文件放入: $reviewDir" "INFO"
        return
    }
    Write-Log ("找到 {0} 个 Excel 文件待评审" -f $excelFiles.Count) "INFO"
    foreach ($f in $excelFiles) {
        Write-Log ("  - {0}" -f $f.Name) "INFO"
    }
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "INAGENT/run_test_review.py"

    if ($exitCode -ne 0) {
        Write-Log "[ERROR] Test review failed." "ERROR"
        Pop-Location
        exit 1
    }

    Write-Log ""
    Write-Log "[SUCCESS] Test review completed. Results: INAGENT/review_results/" "SUCCESS"
}

function Invoke-WebPlatform {
    $webPort = 8010

    # Check if port is already in use and offer to kill the occupying process
    $listening = netstat -ano | Select-String ":$webPort\s.*LISTENING" | ForEach-Object {
        ($_ -split '\s+')[-1]
    } | Sort-Object -Unique
    if ($listening) {
        Write-Log "" "WARN"
        Write-Log "[WARN] Port $webPort is already in use by PID: $($listening -join ', ')" "WARN"
        $answer = Read-Host "Kill existing process(es) and restart? (Y/n)"
        if ($answer -eq '' -or $answer -match '^[Yy]') {
            foreach ($procId in $listening) {
                Write-Log "  Stopping PID $procId ..." "WARN"
                try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {}
            }
            Start-Sleep -Seconds 2
            # Verify port is free
            $still = netstat -ano | Select-String ":$webPort\s.*LISTENING"
            if ($still) {
                Write-Log "[ERROR] Failed to free port $webPort. Please close the process manually." "ERROR"
                return
            }
            Write-Log "[INFO] Port $webPort freed." "INFO"
        } else {
            Write-Log "[INFO] Aborted. Free port $webPort first, then retry." "INFO"
            return
        }
    }

    Write-Log ""
    Write-Log "Starting INAGENT Web Platform (FastAPI + Uvicorn)..." "INFO"
    Write-Log "  URL : http://127.0.0.1:$webPort" "INFO"
    Write-Log "  Docs: http://127.0.0.1:$webPort/docs" "INFO"
    Write-Log ""
    Write-Log "Press Ctrl+C to stop the server." "INFO"
    Write-Log ""

    $exitCode = Invoke-PythonWithLog "-m uvicorn INAGENT.web.app:app --host 0.0.0.0 --port $webPort"

    # Uvicorn returns non-zero on Ctrl+C — treat as normal stop
    if ($exitCode -ne 0) {
        # Check whether the server actually bound successfully by looking for the port
        $portStillUp = netstat -ano | Select-String ":$webPort\s.*LISTENING"
        if (-not $portStillUp) {
            # Server is down — if user pressed Ctrl+C that's fine, otherwise report
            if ($exitCode -eq 3 -or $exitCode -eq -1073741510) {
                # Ctrl+C / SIGINT exit codes on Windows
                Write-Log "" "INFO"
                Write-Log "[INFO] Web platform stopped by user." "INFO"
                return
            }
            Write-Log "" "ERROR"
            Write-Log "[ERROR] Web platform exited with code $exitCode." "ERROR"
            Write-Log "  Possible causes:" "ERROR"
            Write-Log "    - Port $webPort was already in use" "ERROR"
            Write-Log "    - Missing dependency (run: pip install fastapi 'uvicorn[standard]')" "ERROR"
            Write-Log "    - Import error in INAGENT.web.app" "ERROR"
            return
        }
    }

    Write-Log ""
    Write-Log "[INFO] Web platform stopped." "INFO"
}

# Pre-init database when entering menu mode (unless skipped)
if (-not $SkipPreInit -and $Mode -eq "menu") {
    $initSuccess = Invoke-PreInitDatabase
    if (-not $initSuccess) {
        Write-Log "Database initialization failed. Please check error messages." "ERROR"
        Pop-Location
        exit 1
    }
    Write-Log ""
}

if ($Mode -ne "menu") {
    switch ($Mode) {
        "db"          { Invoke-DbAction -Action $DbAction;       Pop-Location; exit 0 }
        "init"        { Invoke-FullInit;                         Pop-Location; exit 0 }
        "interactive" { Invoke-InteractiveConfig;                Pop-Location; exit 0 }
        "e2e"         { Invoke-E2EPipeline;                     Pop-Location; exit 0 }
        "jobs"        { Invoke-Jobs;                             Pop-Location; exit 0 }
        "review"      { Invoke-TestReview;                       Pop-Location; exit 0 }
        "web"         { Invoke-WebPlatform;                      Pop-Location; exit 0 }
        default {
            Write-Log ("[ERROR] Invalid mode: {0}" -f $Mode) "ERROR"
            Pop-Location
            exit 1
        }
    }
}

Write-Host "=========================================="
Write-Host "INAGENT Pipeline - Main Menu"
Write-Host "------------------------------------------"
Write-Host "[1] Database management + GraphRAG"
Write-Host "    (status/create/update/rebuild/delete)"
Write-Host "    Supports: PDF + Office (docx/doc/xlsx/xls) with auto-classification"
Write-Host "[2] Interactive config generation"
Write-Host "    (unified RAG query)"
Write-Host "[3] E2E pipeline (end-to-end execution)"
Write-Host "    Stages 1-4: Plan -> Config -> VM Deploy"
Write-Host "    Stages 5-9: CAMEL Workforce (Deploy/Verify/Traffic/Analysis/Cleanup)"
Write-Host "[4] Full initialization"
Write-Host "    (knowledge base + indexes)"
Write-Host "[5] Jobs processing"
Write-Host "    (config generation only, no execution)"
Write-Host "    Input: INAGENT/jobs/config_tasks/"
Write-Host "[6] Test review (测试用例评审)"
Write-Host "    ReviewPipeline: plan → knowledge → review"
Write-Host "    Input: INAGENT/jobs/test_review/ (.xlsx/.xls)"
Write-Host "[7] Web Platform"
Write-Host "    (browser UI: http://127.0.0.1:8010)"
Write-Host "=========================================="
Write-Host ""
Write-Host "Log file: $LogFile" -ForegroundColor Gray
Write-Host ""

$choice = Read-Host "Please choose an option (1/2/3/4/5/6/7) and press Enter"

switch ($choice) {
    "1" {
        Write-Host ""
        Write-Host "Database management - choose action:" -ForegroundColor Cyan
        Write-Host "[a] status  (database + file classification + GraphRAG status)"
        Write-Host "[b] create  (create DB when missing; auto-classify all files; then GraphRAG)"
        Write-Host "[c] update  (incremental: detect changes, classify, update DB + GraphRAG)"
        Write-Host "[d] rebuild (full rebuild: re-classify all files, rebuild DB + GraphRAG)"
        Write-Host "[e] delete  (delete DB and local index; clear GraphRAG output)"
        Write-Host ""
        Write-Host "Note: create/update/rebuild will also trigger GraphRAG index build." -ForegroundColor Gray
        Write-Host ""
        $dbChoice = Read-Host "Please choose an option (a/b/c/d/e) and press Enter"
        switch ($dbChoice) {
            "a" { Invoke-DbAction -Action "status" }
            "b" { Invoke-DbAction -Action "create" }
            "c" { Invoke-DbAction -Action "update" }
            "d" { Invoke-DbAction -Action "rebuild" }
            "e" { Invoke-DbAction -Action "delete" }
            default {
                Write-Log ("[ERROR] Invalid DB option: {0}" -f $dbChoice) "ERROR"
                Pop-Location
                exit 1
            }
        }
    }
    "2" {
        Invoke-InteractiveConfig
    }
    "3" {
        Invoke-E2EPipeline
    }
    "4" {
        Invoke-FullInit
    }
    "5" {
        Invoke-Jobs
    }
    "6" {
        Invoke-TestReview
    }
    "7" {
        Invoke-WebPlatform
    }
    default {
        Write-Log ("[ERROR] Invalid option: {0}" -f $choice) "ERROR"
        Pop-Location
        exit 1
    }
}

Pop-Location
exit 0

