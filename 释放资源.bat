@echo off
chcp 936 >nul
title 释放资源 - 停服务 + 卸载模型 + 回收显存
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

echo ============================================================
echo   释放资源 —— 出图台 / ComfyUI / 其他本地服务 + Ollama 显存
echo ============================================================
echo.
if not exist "%PY%" (
  echo [错误] 找不到 Python 环境：%PY%
  if /i not "%~1"=="nopause" pause
  exit /b 1
)
"%PY%" "%ROOT%\_release_resources.py"
set "RC=%ERRORLEVEL%"
echo.
echo ------------------------------------------------------------
echo   说明：停掉的服务下次双击 启动出图台.bat 即可恢复；
echo         Ollama 模型卸载后下次调用会自动重载（首次慢一点）。
echo   只想停出图台和 ComfyUI、不动 RAG 服务：
echo         "%PY%" "%ROOT%\_release_resources.py" --ports 8190,8188
echo   只想看看谁占着显存、什么都不停：
echo         "%PY%" "%ROOT%\_release_resources.py" --dry-run
echo ------------------------------------------------------------
echo.
if "%RC%"=="0" (echo [完成] 资源已释放。) else (echo [注意] 有项目未能释放，请看上面的输出。)
echo.
if /i "%~1"=="nopause" exit /b %RC%
pause
exit /b %RC%
