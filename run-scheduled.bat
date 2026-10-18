@echo off
rem 由 cc98-digest 自动生成：计划任务入口。
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=D:\dev\python\python\python 3.12.5\pythonw.exe"
if not exist "logs" mkdir "logs"
for /f %%i in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd') do set LOGDATE=%%i
if "%LOGDATE%"=="" set LOGDATE=unknown
"%PY%" -m cc98digest run >> "logs\scheduler-%LOGDATE%.log" 2>&1
exit /b %ERRORLEVEL%
