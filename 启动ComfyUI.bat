@echo off
setlocal
chcp 936 >nul
title ComfyUI 0.37.0 - Qwen-Image-2.1 (local)
cd /d "%~dp0"

set PORT=8188
set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    pause
    exit /b 1
)

echo ============================================================
echo   ComfyUI 0.37.0  ^|  Qwen-Image-2.1 (GGUF DiT + safetensors TE)
echo.
echo   Web UI  : http://127.0.0.1:%PORT%
echo   Workflows - pick one in the UI sidebar:
echo       qwen_image_2_1_local_t2i        text to image    ~48 s
echo       qwen_image_2_1_local_edit       reference edit  ~157 s
echo       qwen_image_2_1_local_bgremove   background cut   ~60 s
echo.
echo   Outputs : output\
echo   Stop    : press Ctrl+C here, or just close this window
echo ============================================================
echo.
echo Waiting for the server, then opening your browser...
echo (keep this window open while you work)
echo.

rem ---- 启动前端口体检：清掉可能存在的僵死进程 ----
rem 顺序很关键：必须在开浏览器之前。僵死端口的 TCP 也能连上，
rem 不先清理的话浏览器会打开一个永远转圈、连不上后端的页面。
"%PY%" _zombie_check.py %PORT% --name ComfyUI --proc python.exe
if errorlevel 3 goto :already_running
if errorlevel 1 goto :stuck
echo.

rem Poll the TCP port in a hidden PowerShell, then open the default browser.
rem Raw TCP connect instead of HTTP, so the corporate proxy cannot interfere.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$p=%PORT%; for($i=0;$i -lt 300;$i++){ try{ $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $c.Close(); Start-Sleep 2; Start-Process ('http://127.0.0.1:'+$p+'/'); break }catch{ Start-Sleep 1 } }"

rem ---- 出图前清理显存：把 Ollama 驻留在显卡上的模型卸下来 ----
rem 8 GB 卡上 Ollama 和 ComfyUI 抢显存；不清的话编辑档每步会从 9.7s 涨到 100s+
"%PY%" _free_vram.py
echo.

"%PY%" main.py --port %PORT% --listen 127.0.0.1

echo.
echo [ComfyUI exited]
pause
exit /b 0

:already_running
echo.
echo ============================================================
echo   ComfyUI 已经在运行了，不用重复启动。
echo   直接打开：http://127.0.0.1:%PORT%/
echo ============================================================
start "" http://127.0.0.1:%PORT%/
pause
exit /b 0

:stuck
echo.
echo ============================================================
echo   [失败] 端口 %PORT% 被一个清不掉的僵死进程占着，ComfyUI 起不来。
echo.
echo   处理办法（任选）：
echo     1. Ctrl+Shift+Esc 打开任务管理器 - 详细信息 - 结束所有 python.exe，再重跑
echo     2. 重启电脑（最彻底）
echo ============================================================
pause
exit /b 1
