@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\create-shortcut.ps1"
if errorlevel 1 (
  echo.
  echo [!] Failed to create the shortcut.
  pause
  exit /b 1
)
echo.
echo Done. A shortcut named "Steam 中文名编辑器" is now on your Desktop.
pause
