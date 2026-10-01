@echo off
cd /d "%~dp0"
if "%~1"=="" (
 echo Drag a historical CSV file onto IMPORT_HISTORY_WINDOWS.bat
 pause
 exit /b 1
)
where py >nul 2>nul
if %errorlevel%==0 (py -3 import_history.py "%~1") else (python import_history.py "%~1")
pause

