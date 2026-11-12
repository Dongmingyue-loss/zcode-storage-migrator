@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  python app.py
) else (
  start "" pythonw "%~dp0app.py"
)
endlocal
