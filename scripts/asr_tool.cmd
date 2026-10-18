@echo off
rem ASR command line tool: setup / doctor / run / bench. Run it with the Python that has torch installed.
rem   scripts\asr_tool.cmd setup
rem   scripts\asr_tool.cmd doctor --probe
rem   scripts\asr_tool.cmd bench --root D:\old-clips --engines qwen3-asr,funasr --out bench.md
rem   scripts\asr_tool.cmd run D:\some-video.mp4
rem Which Python: set AUTOSLICE_ASR_PYTHON=D:\envs\asr\python.exe first; otherwise "python" from PATH is used.
chcp 65001 >nul
setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
if defined AUTOSLICE_ASR_PYTHON (set "ASR_PY=%AUTOSLICE_ASR_PYTHON%") else (set "ASR_PY=python")
"%ASR_PY%" -X utf8 -m autoslice.desktop.asr %*
endlocal
