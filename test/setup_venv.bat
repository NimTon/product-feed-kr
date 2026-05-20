@echo off
rem Run from test\ ; this script operates on repo root
rem PIP_CONFIG_FILE: config\pip_tsinghua.ini when present (Tsinghua mirror)
rem If no Python: tries winget Microsoft Store Python 3.12, then winget Python.Python.3.12
rem After Store install, if python is still missing: close this window, open a new cmd, run again
chcp 65001 >nul
set "ROOT=%~dp0\.."
cd /d "%ROOT%"

if exist "%ROOT%\config\pip_tsinghua.ini" (
  set "PIP_CONFIG_FILE=%ROOT%\config\pip_tsinghua.ini"
)

set "VENV_PY=%ROOT%\venv\Scripts\python.exe"
set "PYEXE="

call :find_python
if defined PYEXE goto :have_python

echo [setup] Python not found; trying winget (Microsoft Store: Python 3.12)...
where winget >nul 2>nul
if errorlevel 1 (
  echo [setup] winget not found. Install Python 3.12 from Microsoft Store or https://www.python.org (add to PATH), then re-run test\setup_venv.bat.
  pause
  exit /b 1
)

winget install --id 9NCVDN91XZQP -e --source msstore --accept-package-agreements
if errorlevel 1 (
  echo [setup] Store install failed; trying winget source Python.Python.3.12 ...
  winget install -e --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
)

call :find_python
if not defined PYEXE (
  echo [setup] Python still not found in this session. Close this window, open a new Command Prompt, run test\setup_venv.bat again (PATH refresh).
  pause
  exit /b 1
)

:have_python
if exist "%VENV_PY%" (
  echo [setup] venv exists; refreshing deps (pip mirror if PIP_CONFIG_FILE set).
) else (
  echo [setup] Creating venv...
  %PYEXE% -m venv venv
  if not exist "%VENV_PY%" (
    echo [setup] ERROR: Could not create venv. Ensure Python 3.10+ works.
    pause
    exit /b 1
  )
)

echo [setup] upgrading pip...
"%VENV_PY%" -m pip install -U pip setuptools wheel
if errorlevel 1 goto :fail

echo [setup] pip install -r requirements.txt ...
"%VENV_PY%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :fail

if not exist "%ROOT%\config\seven17.json" (
  if exist "%ROOT%\config\seven17.example.json" (
    echo [setup] Tip: copy config\seven17.example.json to config\seven17.json and fill secrets.
  )
)

echo.
echo [setup] Done. Run 01_采集微猫店铺.bat / 02_LLM补全上架信息.bat / 03_上传韩国站正式.bat
echo [setup] Playwright: prefer chrome-win\chrome.exe, else system Chrome/Edge.
echo [setup] Offline wheels: pip download -r requirements.txt -d wheels
echo            then: venv\Scripts\pip install --no-index --find-links=wheels -r requirements.txt
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
