@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if "%~1"=="" (python pipeline.py all) else (python pipeline.py %*)
pause
