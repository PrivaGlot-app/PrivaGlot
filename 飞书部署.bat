@echo off
rem ============================================================
rem  PrivaGlot feishu (bot) deploy (Windows)
rem  Double-click this file. All real work happens in
rem  setup\bootstrap.ps1 (UTF-8 with BOM). This .bat itself
rem  is kept pure ASCII on purpose: a .bat with non-ASCII
rem  text breaks under some console code pages.
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\bootstrap.ps1" -Mode feishu %*
echo.
echo  (The service has stopped. This window can be closed.)
echo.
pause
