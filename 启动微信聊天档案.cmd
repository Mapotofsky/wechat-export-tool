@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" "%~dp0app\desktop.py" %*
  exit /b
)
set "WORKBENCH=%~dp0dist\微信聊天档案 完整语音版"
if not exist "%WORKBENCH%\runtime\pythonw.exe" set "WORKBENCH=%~dp0dist\微信聊天档案 基础版"
if not exist "%WORKBENCH%\runtime\pythonw.exe" (
  echo 未找到便携运行时。请先解压发布包，再双击包内的启动微信聊天档案.cmd。
  echo 源码开发启动与构建方法请参阅 README.md。
  pause
  exit /b 1
)
if not defined WECHAT_ARCHIVE_DATA_DIR set "WECHAT_ARCHIVE_DATA_DIR=%~dp0local_data"
start "" "%WORKBENCH%\runtime\pythonw.exe" "%WORKBENCH%\app\desktop.py" %*
exit /b
