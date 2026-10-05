@echo off
echo ===========================================================================
echo   SACHIN QUANT: TASK SCHEDULER ELEVATION TO SYSTEM ^& HIGHEST RUN LEVEL
echo ===========================================================================

:: Reconfigure task to run under SYSTEM with HIGHEST run level
schtasks /change /tn "SachinQuant_LiveScheduler" /ru "SYSTEM" /rl HIGHEST

:: Configure WakeToRun and power flags via PowerShell
powershell -NoProfile -Command "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable; Set-ScheduledTask -TaskName 'SachinQuant_LiveScheduler' -Settings $settings"

:: Query and display verified status
echo.
echo [VERIFIED TASK CONFIGURATION]
schtasks /query /tn "SachinQuant_LiveScheduler" /fo LIST /v

echo.
echo [DONE] Task reconfigured successfully.
