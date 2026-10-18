@echo off
rem 由 cc98-digest 自动生成：手动跑一次（前台可见输出）。
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=D:\dev\python\python\python 3.12.5\pythonw.exe"
if not exist "logs" mkdir "logs"
%PY% -m cc98digest once --print %*
exit /b %ERRORLEVEL%
