@echo off
setlocal
cd /d "%~dp0"
set "BUNDLED_PY=C:\Users\30367\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PY%" (
  "%BUNDLED_PY%" unified_server.py
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 unified_server.py
  ) else (
    python unified_server.py
  )
)
pause

