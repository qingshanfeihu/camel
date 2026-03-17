@echo off
REM Run LLM Gateway tests

echo ========================================
echo INFOAGEN LLM Gateway Test Suite
echo ========================================
echo.

REM Check if gateway is running
echo [1/2] Checking if gateway is running...
curl -s http://localhost:8000/health >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Error: Gateway is not running at http://localhost:8000
    echo Please start the gateway first: llm_gateway\deploy.bat
    exit /b 1
)
echo OK - Gateway is running

echo.
echo [2/2] Running test suite...
python llm_gateway\test_gateway.py

echo.
echo ========================================
echo Test completed
echo ========================================
echo.
echo To view gateway logs: podman logs -f infoagen-llm-gateway
echo To view metrics: curl http://localhost:8000/metrics
echo.
