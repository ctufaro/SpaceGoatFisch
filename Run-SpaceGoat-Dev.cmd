@echo off
cd /d "%~dp0"
"%~dp0.spacegoat-build\Scripts\python.exe" "%~dp0spacegoat_fisch_server.py" --host 127.0.0.1 --data-dir "%~dp0spacegoat-dev-data"
if errorlevel 1 pause
