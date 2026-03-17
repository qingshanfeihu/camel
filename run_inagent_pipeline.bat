@echo off
chcp 65001 >nul 2>&1
REM ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
REM INFOAGEN Pipeline Launcher for Windows
REM
REM This script provides a unified entry point to the INFOAGEN pipeline:
REM   1. Database + GraphRAG management (status/create/update/rebuild/delete)
REM   2. Interactive config generation (unified RAG)
REM   3. E2E pipeline (端到端自动化测试流水线)
REM   4. Full initialization (workflow steps)
REM   5. Jobs processing (workflow config generation)
REM   6. Test review (测试用例评审 via ReviewPipeline)
REM   7. Web platform (browser UI)
REM
REM Pipeline Stages (e2e mode):
REM   Stage 1    -> Test Plan Generation (RAG + Agent)
REM   Stage 2    -> Network Environment Planning
REM   Stage 3    -> Task Decomposition & Config Generation
REM   Stage 3.5  -> VIP Conflict Pre-check
REM   Stage 4    -> VM Test Environment Deployment
REM   Stage 5-9  -> CAMEL Workforce Pipeline (multi-agent parallel)
REM       5 - DeployWorker      (Config Push via SSH)
REM       6 - TrafficWorker     (HTTP Verify + Fault Injection)
REM       7 - VerifyShowWorker  (SSH Health Check)
REM       8 - AnalysisWorker    (AI Verdict: PASS/FAIL)
REM       9 - CleanupWorker     (Environment Cleanup)
REM
REM Usage:
REM   run_inagent_pipeline.bat                 (Interactive menu)
REM   run_inagent_pipeline.bat -Mode e2e      (Run E2E pipeline directly)
REM   run_inagent_pipeline.bat -Mode review    (Run test case review)
REM   run_inagent_pipeline.bat -Mode db -DbAction create
REM ================================================================================

setlocal EnableExtensions

REM Ensure UTF-8 for Python subprocess output
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Run from repo root
pushd %~dp0

echo.
echo ================================================================================
echo INFOAGEN Pipeline Launcher
echo ================================================================================
echo.

REM Delegate to PowerShell script for proper Unicode rendering and interactive support
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '%~dp0INAGENT\run_inagent_pipeline.ps1' %*"
set RETCODE=%errorlevel%

popd
endlocal
exit /b %RETCODE%
