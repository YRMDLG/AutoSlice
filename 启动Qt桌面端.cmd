@echo off
setlocal
set "QT_ENV=%LOCALAPPDATA%\AutoSlice\qt-preview-venv"
for /f "delims=" %%I in ('py -3.10 -c "import site; print(site.getsitepackages()[-1])"') do set "BASE_SITE=%%I"
set "PYTHONPATH=%~dp0src;%BASE_SITE%;%PYTHONPATH%"
if not exist "%QT_ENV%\Scripts\python.exe" (
    py -3.10 -m venv "%QT_ENV%"
    if errorlevel 1 exit /b 1
)
"%QT_ENV%\Scripts\python.exe" -c "import PySide6, requests" >nul 2>nul
if errorlevel 1 (
    "%QT_ENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements-qt-desktop.txt"
    if errorlevel 1 exit /b 1
)
cd /d "%~dp0"
"%QT_ENV%\Scripts\python.exe" -m autoslice.desktop.qt_app
endlocal
