@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  set PY=venv\Scripts\python.exe
) else (
  set PY=python
)
echo 启动商品库浏览 http://127.0.0.1:8765/
echo   工具栏：分类配对 / 无价白名单 / 不上架诊断
start "" "http://127.0.0.1:8765/"
%PY% -m pip install -q flask 2>nul
%PY% -m product_feed_kr.pf_browser
pause
