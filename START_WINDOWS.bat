@echo off
setlocal
cd /d "%~dp0"
echo [PCR v11] Starting integrated dashboard...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pip install --user pdfplumber >nul 2>nul
  py -3 app.py
) else (
  python -m pip install --user pdfplumber >nul 2>nul
  python app.py
)
pause

