@echo off
chcp 936 >nul
title Qwen-Image-2.1 出图台 - 启动
setlocal
rem ---- 根目录取脚本自身所在位置，换机器 / 换盘符不用改这一行 ----
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "COMPY_PORT=8188"
set "WEB_PORT=8190"
set "OLLAMA_PORT=11434"
set "OLLAMA_APP=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

rem ---- 启动模式：默认局域网可访问。想只监听本机，双击「启动出图台-仅本机.bat」----
rem ---- 也支持外部先设好 WEB_HOST 再调本脚本 ----
if not defined WEB_HOST set "WEB_HOST=0.0.0.0"

if not exist "%PY%" goto :no_py
cd /d "%ROOT%"

echo ============================================================
echo    Qwen-Image-2.1 本地出图台
echo ============================================================
if not "%WEB_HOST%"=="0.0.0.0" goto :mode_local
echo    模式：局域网可访问（0.0.0.0）
goto :mode_done
:mode_local
echo    模式：仅本机（127.0.0.1）
:mode_done
echo.

rem ---------- 1) Ollama（提示词扩写用） ----------
netstat -ano | findstr ":%OLLAMA_PORT% " | findstr LISTENING >nul
if not errorlevel 1 goto :ollama_ok
echo [1/5] 启动 Ollama（提示词扩写用）...
if exist "%OLLAMA_APP%" goto :o_app
start "Ollama 服务" /min "%OLLAMA_EXE%" serve
goto :o_wait

:o_app
start "" "%OLLAMA_APP%"

:o_wait
set /a O=0
:o_loop
netstat -ano | findstr ":%OLLAMA_PORT% " | findstr LISTENING >nul
if not errorlevel 1 goto :ollama_ready
set /a O+=1
if %O% GEQ 30 goto :ollama_timeout
timeout /t 1 /nobreak >nul
goto :o_loop

:ollama_timeout
echo       [警告] Ollama 30 秒内未就绪。
echo       出图不受影响，但「扩写 / 维度」会退化成离线规则版（质量差很多）。
echo       手动启动：%OLLAMA_APP%
goto :comfy_check

:ollama_ready
echo       就绪。
goto :comfy_check

:ollama_ok
echo [1/5] Ollama 已在运行（端口 %OLLAMA_PORT%）

rem ---------- 2) 清理显存（卸载 Ollama 占着的 GPU 模型） ----------
echo [2/5] 清理显存（卸载 Ollama 驻留在显卡上的模型）...
"%PY%" _free_vram.py
if errorlevel 1 echo       [提示] 清理脚本没跑通，不影响出图，继续。
echo.

rem ---------- 3) ComfyUI 推理后端 ----------
:comfy_check
rem 端口在监听 != 服务活着。僵死进程会永久挡住启动（2026-10-02 事故：点了三次都起不来）
"%PY%" _zombie_check.py %COMPY_PORT% --name ComfyUI --proc python.exe
if errorlevel 3 goto :comfy_ok
if errorlevel 1 goto :comfy_stuck
echo [3/5] 启动 ComfyUI 推理后端（首次加载模型约 30 秒）...
start "ComfyUI 后端" /min "%PY%" main.py --port %COMPY_PORT% --listen 127.0.0.1
goto :wait_comfy

:comfy_ok
echo [3/5] ComfyUI 后端已在运行（端口 %COMPY_PORT%）
goto :start_web

:wait_comfy
set /a N=0
:wc_loop
netstat -ano | findstr ":%COMPY_PORT% " | findstr LISTENING >nul
if not errorlevel 1 goto :comfy_ready
set /a N+=1
if %N% GEQ 90 goto :comfy_timeout
timeout /t 1 /nobreak >nul
goto :wc_loop

:comfy_ready
echo       后端就绪。
goto :start_web

:comfy_timeout
echo       [警告] ComfyUI 90 秒内未就绪，仍会打开网页，出图时会自动重试。
goto :start_web

:comfy_stuck
echo       [失败] 端口 %COMPY_PORT% 被一个清不掉的僵死进程占着，ComfyUI 起不来。
echo              处理办法：Ctrl+Shift+Esc 打开任务管理器 - 详细信息 - 结束所有 python.exe，
echo              然后重新双击本脚本。还不行就重启电脑（最彻底）。
echo.
echo              现在仍会打开网页，但顶部的「ComfyUI 在线」指示灯会是红的。
goto :start_web

rem ---------- 4) 网页服务 ----------
:start_web
rem 若已占用 8190 的旧服务监听地址与本次模式不一致，先停掉再重启
set "WANT=127.0.0.1:%WEB_PORT%"
if "%WEB_HOST%"=="0.0.0.0" set "WANT=0.0.0.0:%WEB_PORT%"
netstat -ano | findstr ":%WEB_PORT% " | findstr LISTENING >nul
if errorlevel 1 goto :sw_check
netstat -ano | findstr ":%WEB_PORT% " | findstr LISTENING | findstr /c:"%WANT%" >nul
if not errorlevel 1 goto :web_ok
echo       [提示] 旧服务监听地址与当前模式不符，正在重启...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%WEB_PORT% " ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>nul
timeout /t 2 /nobreak >nul
:sw_check
netstat -ano | findstr ":%WEB_PORT% " | findstr LISTENING >nul
if not errorlevel 1 goto :web_ok
echo [4/5] 启动出图台网页服务（端口 %WEB_PORT%）...
start "Qwen21 出图台服务" /min "%PY%" _server.py
goto :wait_web

:web_ok
echo [4/5] 网页服务已在运行（端口 %WEB_PORT%）
goto :open

:wait_web
set /a M=0
:ww_loop
netstat -ano | findstr ":%WEB_PORT% " | findstr LISTENING >nul
if not errorlevel 1 goto :open
set /a M+=1
if %M% GEQ 30 goto :open
timeout /t 1 /nobreak >nul
goto :ww_loop

rem ---------- 5) 打开浏览器 ----------
:open
rem ---- 先把局域网地址打出来，再开浏览器 ----
rem ---- 浏览器一弹出就抢走焦点，控制台被挡在后面，用户根本看不到这个地址 ----
set "LANURL="
if not "%WEB_HOST%"=="0.0.0.0" goto :open_browser
if exist "%ROOT%\_lan_url.txt" set /p LANURL=<"%ROOT%\_lan_url.txt"
if not defined LANURL set "LANURL=（没探测到，见上方服务窗口的「局域网访问」）"
echo.
echo ============================================================
echo   手机 / 平板访问（需连同一个 WiFi）：
echo.
echo        %LANURL%
echo.
echo   在手机浏览器地址栏直接输入上面这串即可。
echo   打不开就先放行防火墙：右键「放行出图台端口.bat」
echo   选「以管理员身份运行」，只需做一次。
echo ============================================================
:open_browser
echo [5/5] 本机浏览器打开 http://127.0.0.1:%WEB_PORT%
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:%WEB_PORT%
:open_done
echo.
echo ------------------------------------------------------------
echo  完成。最小化的三个窗口（Ollama / ComfyUI 后端 / 出图台服务）
echo  请勿关闭。
echo  需要停止服务，双击「停止出图台.bat」。
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:no_py
echo [错误] 找不到 Python 环境：%PY%
echo 请确认 C:\ComfyUI\.venv 存在。
pause
exit /b 1
