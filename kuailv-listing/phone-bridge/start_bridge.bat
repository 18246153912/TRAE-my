@echo off
chcp 65001 >nul
title 快驴手机桥接服务 (关闭此窗口即停止服务)
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel% neq 0 (
  echo [错误] 未找到 python，请先安装 Python 3 并勾选 Add to PATH
  pause
  exit /b 1
)
python phone_bridge.py
pause
