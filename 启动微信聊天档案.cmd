@echo off
chcp 65001 >nul
cd /d "%~dp0"
"D:\efficiency\anaconda3\envs\appdev\python.exe" run.py web --open-browser
if errorlevel 1 pause
