@echo off
REM Deploy LLM Gateway using Podman

echo ========================================
echo INFOAGEN LLM Gateway Deployment
echo ========================================
echo.

REM Check if podman is installed
where podman >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Error: Podman is not installed or not in PATH
    echo Please install Podman first: https://podman.io/
    exit /b 1
)

echo [1/5] Checking environment file...
if not exist "INAGENT\.env" (
    echo Error: INAGENT\.env file not found
    exit /b 1
)
echo OK - Environment file found

echo.
echo [2/5] Stopping existing containers...
podman-compose -f llm_gateway\podman-compose.yml down
echo OK

echo.
echo [3/5] Building gateway image...
podman-compose -f llm_gateway\podman-compose.yml build
if %ERRORLEVEL% NEQ 0 (
    echo Error: Failed to build image
    exit /b 1
)
echo OK

echo.
echo [4/5] Starting gateway service...
podman-compose -f llm_gateway\podman-compose.yml up -d
if %ERRORLEVEL% NEQ 0 (
    echo Error: Failed to start service
    exit /b 1
)
echo OK

echo.
echo [5/5] Waiting for service to be ready...
timeout /t 10 /nobreak >nul

echo.
echo ========================================
echo Gateway Service Status
echo ========================================
podman ps --filter "name=infoagen-llm-gateway"

echo.
echo ========================================
echo Service is running at: http://localhost:9000
echo Health check: http://localhost:9000/health
echo Metrics: http://localhost:9000/metrics
echo ========================================
echo.
echo To view logs: podman logs -f infoagen-llm-gateway
echo To stop service: podman-compose -f llm_gateway\podman-compose.yml down
echo To run tests: python llm_gateway\test_gateway.py
echo.
