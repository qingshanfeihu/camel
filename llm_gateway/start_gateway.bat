@echo off
chcp 65001 >nul
REM ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
REM LLM Gateway Startup Script for Windows
REM
REM This script starts the unified LLM Gateway service for INFOAGEN.
REM
REM Features:
REM   - Multi-model concurrent calling (GLM-Z1-9B, Qwen3-8B, DeepSeek-R1-Qwen3-8B)
REM   - Race mode: parallel calls, fastest response wins
REM   - Three-level rate limiting (global, provider, model)
REM   - OpenAI-compatible API interface
REM
REM Endpoints:
REM   - Health check:    http://localhost:9000/health
REM   - Chat completion: http://localhost:9000/v1/chat/completions
REM   - Embeddings:      http://localhost:9000/v1/embeddings
REM   - Metrics:         http://localhost:9000/metrics
REM
REM Usage:
REM   start_gateway.bat              (Start gateway on port 9000)
REM   Ctrl+C or close window to stop
REM
REM Prerequisites:
REM   - Python with required packages (see requirements-gateway.txt)
REM   - INAGENT/.env with SILICONFLOW_API_KEY configured
REM
REM Flow in INFOAGEN:
REM   LLM Gateway (this) -> INAGENT modules -> RAG/Workflow processing
REM ================================================================================

setlocal enabledelayedexpansion

echo ================================================================================
echo INFOAGEN LLM Gateway Startup
echo ================================================================================
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

REM Check if Python is available
for /f "tokens=*" %%i in ('where python') do set "PYTHON_EXE=%%i" & goto :found_python
:found_python
if not defined PYTHON_EXE (
    echo Error: Python not found in PATH
    pause
    exit /b 1
)
echo Python found at: %PYTHON_EXE%

REM Set working directory
cd /d "%~dp0.."
echo Working directory: %CD%
echo.

REM Set log directory
set LOG_DIR=%CD%\llm_gateway\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Generate log filename with timestamp
for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c%%a%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a%%b)
set LOGFILE=%LOG_DIR%\gateway_%mydate%_%mytime%.log

echo Log file: %LOGFILE%
echo.
set "GATEWAY_PORT=9000"

REM Check if port is in use
set "PORT_PIDS="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%GATEWAY_PORT%" ^| findstr "LISTENING"') do (
    if not "%%p"=="0" (
        set "PORT_PIDS=!PORT_PIDS! %%p"
    )
)

if defined PORT_PIDS (
    echo.
    echo [WARNING] Port %GATEWAY_PORT% is already in use by PID^(s^):%PORT_PIDS%
    echo Do you want to terminate these processes and continue?
    choice /C YN /M "Press Y to continue or N to abort"
    if errorlevel 2 (
        echo Aborted by user. Please free port %GATEWAY_PORT% and retry.
        pause
        exit /b 1
    )

    for %%p in (!PORT_PIDS!) do (
        echo Terminating PID %%p...
        taskkill /F /PID %%p >nul 2>&1
    )

    REM Wait for port to be fully released (Windows needs time)
    echo Waiting for port to be released...
    set WAIT_ATTEMPTS=0
    :wait_loop
    timeout /t 1 /nobreak >nul
    set /a WAIT_ATTEMPTS+=1
    
    REM Check if port is still in use
    set "PORT_STILL_IN_USE="
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%GATEWAY_PORT%" ^| findstr "LISTENING"') do (
        set "PORT_STILL_IN_USE=1"
        goto :still_waiting
    )
    goto :port_free
    
    :still_waiting
    if !WAIT_ATTEMPTS! LSS 10 (
        goto :wait_loop
    ) else (
        echo Failed to free port %GATEWAY_PORT% after 10 seconds.
        pause
        exit /b 1
    )
    
    :port_free
    echo Port %GATEWAY_PORT% is now free.
)

echo Starting LLM Gateway on port %GATEWAY_PORT%...
echo Press Ctrl+C to stop the service
echo ================================================================================
echo.

REM Start the gateway with auto-restart and Enter-to-stop (PowerShell runner)
set "PS1_SCRIPT=%~dp0start_gateway_simple.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_SCRIPT%" -LogFile "%LOGFILE%" -PythonExe "%PYTHON_EXE%"

if %errorlevel% neq 0 (
        echo.
        echo ================================================================================
        echo Gateway stopped with error code: %errorlevel%
        echo ================================================================================
        pause
        exit /b %errorlevel%
)

echo.
echo ================================================================================
echo Gateway stopped normally
echo ================================================================================
pause
