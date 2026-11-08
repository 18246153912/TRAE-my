@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0新客开通工具.py"
) else (
    python "%~dp0新客开通工具.py"
)
