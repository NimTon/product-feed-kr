@echo off
rem 首次分发：在仓库根目录双击本脚本，生成 venv 并安装 requirements.txt
rem - pip 使用清华镜像：config\pip_tsinghua.ini（通过 PIP_CONFIG_FILE）
rem - 若本机无 Python：优先 winget 从 Microsoft Store 安装 Python 3.12；失败则回退 winget 官方源 Python.Python.3.12
rem - 商店安装后若仍找不到 python，请关闭本窗口，新开 cmd 再运行本脚本一次（PATH 刷新）
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0config\pip_tsinghua.ini" (
  set "PIP_CONFIG_FILE=%~dp0config\pip_tsinghua.ini"
)

set "VENV_PY=%~dp0venv\Scripts\python.exe"
set "PYEXE="

call :find_python
if defined PYEXE goto :have_python

echo [setup] 未检测到 Python，尝试用 winget 安装（Microsoft Store：Python 3.12）...
where winget >nul 2>nul
if errorlevel 1 (
  echo [setup] 本机无 winget。请从 Microsoft Store 搜索「Python 3.12」安装，或从 https://www.python.org 安装并勾选 Add to PATH，然后重新运行本脚本。
  pause
  exit /b 1
)

winget install --id 9NCVDN91XZQP -e --source msstore --accept-package-agreements
if errorlevel 1 (
  echo [setup] Store 安装未成功，尝试 winget 源 Python.Python.3.12 ...
  winget install -e --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
)

call :find_python
if not defined PYEXE (
  echo [setup] 安装后当前窗口仍找不到 Python。请关闭本窗口，新开「命令提示符」再运行 setup_venv.bat。
  pause
  exit /b 1
)

:have_python
if exist "%VENV_PY%" (
  echo [setup] 已存在 venv，将刷新依赖（pip：清华源）。
) else (
  echo [setup] 正在创建 venv...
  %PYEXE% -m venv venv
  if not exist "%VENV_PY%" (
    echo [setup] ERROR: 无法创建 venv。请确认 Python 3.10+ 可用。
    pause
    exit /b 1
  )
)

echo [setup] upgrading pip...
"%VENV_PY%" -m pip install -U pip setuptools wheel
if errorlevel 1 goto :fail

echo [setup] pip install -r requirements.txt ...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail

if not exist "%~dp0config\seven17.json" (
  if exist "%~dp0config\seven17.example.json" (
    echo [setup] 提示：复制 config\seven17.example.json 为 config\seven17.json 并填写密钥。
  )
)

echo.
echo [setup] 完成。可运行 scrape_wecatalog_store.bat / llm_enrich_sqlite.bat / upload_*.bat
echo [setup] Playwright：优先使用 chrome-win\chrome.exe，否则使用本机 Chrome/Edge。
echo [setup] 离线包：pip download -r requirements.txt -d wheels
echo            然后：venv\Scripts\pip install --no-index --find-links=wheels -r requirements.txt
echo.
pause
exit /b 0

:fail
echo [setup] FAILED.
pause
exit /b 1

:find_python
set "PYEXE="
py -3 -c "import sys; assert sys.version_info >= (3, 10)" 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE exit /b 0
python -c "import sys; assert sys.version_info >= (3, 10)" 2>nul
if not errorlevel 1 set "PYEXE=python"
exit /b 0
