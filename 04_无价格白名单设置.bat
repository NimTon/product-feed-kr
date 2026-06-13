@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  set PY=venv\Scripts\python.exe
) else (
  set PY=python
)
echo [04] 无价白名单已并入 05 商品库浏览 — 请点击工具栏「无价白名单」
start "" "http://127.0.0.1:8765/#no-price-whitelist"
%PY% -m pip install -q flask 2>nul
%PY% -m product_feed_kr.pf_browser
pause
