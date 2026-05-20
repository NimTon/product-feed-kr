@echo off
chcp 65001 >nul

if /i not "%~1"=="__hidden__" (
  powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -FilePath '%ComSpec%' -ArgumentList '/c """"%~f0"""" __hidden__' -WindowStyle Hidden"
  exit /b
)
shift /1

cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0venv\Scripts\pythonw.exe" (
  set "PYEXE=%~dp0venv\Scripts\pythonw.exe"
) else if exist "%~dp0venv\Scripts\python.exe" (
  set "PYEXE=%~dp0venv\Scripts\python.exe"
)
if not defined PYEXE (
  py -3 -c "import sys; print(sys.executable)" >nul 2>nul
  if not errorlevel 1 (
    set "PYEXE=py -3"
  ) else (
    set "PYEXE=python"
  )
)

%PYEXE% -m product_feed_kr.seven17_no_price_whitelist_gui
if errorlevel 1 (
  echo.
  echo [GUI] 启动失败，请先运行 test\setup_venv.bat 安装依赖。
  pause
)
