@echo off
setlocal
set "PREVIEW_ENV=%LOCALAPPDATA%\AutoSlice\qt-preview-venv"
if not exist "%PREVIEW_ENV%\Scripts\python.exe" (
    py -3.10 -m venv "%PREVIEW_ENV%"
    if errorlevel 1 exit /b 1
)
"%PREVIEW_ENV%\Scripts\python.exe" -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    "%PREVIEW_ENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements-qt-preview.txt"
    if errorlevel 1 exit /b 1
)
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
cd /d "%~dp0"
"%PREVIEW_ENV%\Scripts\python.exe" -m autoslice.desktop.qt_preview
endlocal
