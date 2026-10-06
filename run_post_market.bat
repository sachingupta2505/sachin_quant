@echo off
setlocal

cd /d "C:\sachin_quant"

if not exist "logs" mkdir "logs"

if exist ".venv\Scripts\activate" (
    call ".venv\Scripts\activate"
) else if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

python -m agents.post_market_ai_chief >> "logs\post_market_%DATE%.log" 2>&1

endlocal
