@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

python "启动.py"
if errorlevel 1 (
  echo.
  echo AutoCover 启动失败，请检查上方错误信息。
  pause
)
