@echo off
chcp 936 >nul
title 清理显存 - 卸载 Ollama 占用的显存
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

echo ============================================================
echo   清理显存 —— 卸载 Ollama 驻留在显卡上的模型
echo ============================================================
echo.
if not exist "%PY%" (
  echo [错误] 找不到 Python 环境：%PY%
  if /i not "%~1"=="nopause" pause
  exit /b 1
)
"%PY%" "%ROOT%\_free_vram.py"
echo.
echo ------------------------------------------------------------
echo   说明：Ollama 的模型卸载后，下次调用会自动重新加载（首次慢一点）。
echo         出图前跑一次，ComfyUI 就能拿到全部显存。
echo         8 GB 卡上这一步很关键 —— 实测不清的话，参考图编辑档
echo         会从 38 秒变成 12 分钟。
echo ------------------------------------------------------------
echo.
if /i "%~1"=="nopause" exit /b 0
pause
exit /b 0
