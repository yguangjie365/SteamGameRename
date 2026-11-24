@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYCMD="
where python >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
  where py >nul 2>&1 && set "PYCMD=py -3"
)
if not defined PYCMD (
  echo [!] Python was not found in PATH.
  echo     Install Python 3.11+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

%PYCMD% -X utf8 -c "import webview, pystray, PIL" >nul 2>&1
if errorlevel 1 (
  echo [i] Desktop dependencies are missing. Installing...
  %PYCMD% -m pip install pywebview pystray Pillow
  if errorlevel 1 (
    echo [!] Install failed. The editor will start in browser mode instead.
    echo     Manual command: pip install pywebview pystray Pillow
    %PYCMD% -X utf8 "%~dp0run.py"
    goto :done
  )
)

%PYCMD% -X utf8 "%~dp0run_desktop.py" %*

:done
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [!] Desktop launcher exited with code %CODE%.
  pause
)
exit /b %CODE%
