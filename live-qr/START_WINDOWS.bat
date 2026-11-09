@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
 echo Python 3 and Internet access are required for the first installation.
 pause
 exit /b 1
)
%PY% app.py
pause
