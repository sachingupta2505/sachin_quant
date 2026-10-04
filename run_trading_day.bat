@echo off
setlocal

:: Navigate to project root directory
cd /d "C:\sachin_quant"

:: Ensure logs directory exists
if not exist "logs" mkdir "logs"

:: Activate virtual environment if present
if exist ".venv\Scripts\activate" (
    call ".venv\Scripts\activate"
) else if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

:: Run main_runner.py with outputs redirected to timestamped log files in logs/daily_trading_%DATE%.log
python main_runner.py >> "logs\daily_trading_%DATE%.log" 2>&1

endlocal
