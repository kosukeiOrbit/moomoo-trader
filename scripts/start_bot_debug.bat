@echo off
cd /d C:\work\git\moomoo-trader
echo [%date% %time%] Starting Bot... >> C:\work\git\moomoo-trader\logs\bat_debug.log
C:\work\git\moomoo-trader\venv\Scripts\python.exe C:\work\git\moomoo-trader\src\main.py >> C:\work\git\moomoo-trader\logs\bat_debug.log 2>&1
echo [%date% %time%] Bot exited with code %ERRORLEVEL% >> C:\work\git\moomoo-trader\logs\bat_debug.log
