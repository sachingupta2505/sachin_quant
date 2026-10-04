@echo off
title SACCHIN QUANT - EVENT-DRIVEN BLACKBOARD LAUNCHER
echo =========================================================================
echo  LAUNCHING 4 INDEPENDENT AGENTS VIA BLACKBOARD PATTERN (system_bus.db)
echo =========================================================================
echo.

cd /d "C:\sachin_quant"

:: Reset blackboard state for clean startup
if exist "system_bus.db" del /f /q "system_bus.db"
if exist "system_bus.db-wal" del /f /q "system_bus.db-wal"
if exist "system_bus.db-shm" del /f /q "system_bus.db-shm"

echo [1/4] Starting Agent 3: AUDITOR (Listens for ORDER_PROPOSED -> ORDER_APPROVED/VETOED)...
start "Agent 3: AUDITOR (Blackboard)" cmd /k "title Agent 3: AUDITOR && cd /d C:\sachin_quant && python run_auditor.py"
timeout /t 1 /nobreak >nul

echo [2/4] Starting Agent 4: DEVOPS (Listens for ORDER_APPROVED -> ORDER_EXECUTED)...
start "Agent 4: DEVOPS (Blackboard)" cmd /k "title Agent 4: DEVOPS && cd /d C:\sachin_quant && python run_devops.py"
timeout /t 1 /nobreak >nul

echo [3/4] Starting Agent 2: CODER (Listens for SIGNAL_DETECTED -> ORDER_PROPOSED)...
start "Agent 2: CODER (Blackboard)" cmd /k "title Agent 2: CODER && cd /d C:\sachin_quant && python run_coder.py"
timeout /t 1 /nobreak >nul

echo [4/4] Starting Agent 1: ARCHITECT (Tracks 5m & IB -> Publishes SIGNAL_DETECTED)...
start "Agent 1: ARCHITECT (Blackboard)" cmd /k "title Agent 1: ARCHITECT && cd /d C:\sachin_quant && python run_architect.py"

echo.
echo =========================================================================
echo  ALL 4 BLACKBOARD AGENTS RUNNING IN SEPARATE TERMINALS!
echo   * ARCHITECT: Publishes [SIGNAL_DETECTED]
echo   * CODER:     Consumes [SIGNAL_DETECTED]  -> Publishes [ORDER_PROPOSED]
echo   * AUDITOR:   Consumes [ORDER_PROPOSED]   -> Publishes [ORDER_APPROVED/VETOED]
echo   * DEVOPS:    Consumes [ORDER_APPROVED]   -> Publishes [ORDER_EXECUTED]
echo =========================================================================
pause
