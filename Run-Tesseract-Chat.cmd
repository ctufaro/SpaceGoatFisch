@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tesseract_chat_reader.py" %*
) else if exist "%~dp0.spacegoat-build\Scripts\python.exe" (
    "%~dp0.spacegoat-build\Scripts\python.exe" "%~dp0tesseract_chat_reader.py" %*
) else (
    py -3 "%~dp0tesseract_chat_reader.py" %*
)
if errorlevel 1 pause
