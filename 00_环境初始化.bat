@echo off
rem 首次：创建 venv 并安装依赖；已有 venv：仅 pip install -r requirements.txt
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0config\pip_tsinghua.ini" (
  set "PIP_CONFIG_FILE=%~dp0config\pip_tsinghua.ini"
)

set "VENV_PY=%~dp0venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  echo [00] 已存在 venv，刷新依赖...
  goto :install_deps
)

echo [00] 未找到 venv，正在创建...
set "PYEXE="
py -3 -c "import sys; assert sys.version_info >= (3, 10)" 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys; assert sys.version_info >= (3, 10)" 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo [00] 错误：需要 Python 3.10+（py -3 或 python 在 PATH 中）。
  echo       可运行 test\setup_venv.bat 尝试 winget 安装 Python。
  pause
  exit /b 1
)

%PYEXE% -m venv venv
if not exist "%VENV_PY%" (
  echo [00] 错误：创建 venv 失败。
  pause
  exit /b 1
)
echo [00] venv 已创建。

:install_deps
echo [00] 升级 pip...
"%VENV_PY%" -m pip install -U pip setuptools wheel
if errorlevel 1 goto :fail

echo [00] pip install -r requirements.txt ...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail

if not exist "%~dp0config\seven17.json" (
  if exist "%~dp0config\seven17.example.json" (
    echo [00] 提示：复制 config\seven17.example.json 为 config\seven17.json 并填写账号。
  )
)

echo.
echo [00] 完成。可运行 01_采集微猫店铺.bat / 02_LLM补全上架信息.bat / 03_上传韩国站正式.bat
echo [00] 首次使用 Playwright 可执行：venv\Scripts\python.exe -m playwright install chromium
echo.
pause
exit /b 0

:fail
echo [00] 安装失败。
pause
exit /b 1
