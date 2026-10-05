@echo off
chcp 936 >nul
title Qwen-Image-2.1 出图台 - 停止
setlocal
set "COMPY_PORT=8188"
set "WEB_PORT=8190"
set "OLLAMA_PORT=11434"

echo ============================================================
echo    停止 Qwen-Image-2.1 出图台
echo ============================================================
echo.

rem ---------- 1) 网页服务（必停） ----------
set "K1=0"
echo [1/3] 停止出图台网页服务（端口 %WEB_PORT%）...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%WEB_PORT% " ^| findstr LISTENING') do (
  taskkill /F /PID %%P >nul 2>nul
  set "K1=1"
)
if "%K1%"=="0" ( echo       端口 %WEB_PORT% 没有运行中的服务 ) else ( echo       已停止 )

rem ---------- 2) ComfyUI 后端（可选） ----------
echo.
echo [2/3] ComfyUI 推理后端（端口 %COMPY_PORT%）
echo       保留它可以让下次出图秒开；停止它会释放全部显存。
set "A2="
set /p A2=       要一起停止吗？输入 Y 停止，直接回车保留:
if /i not "%A2%"=="Y" goto :skip_comfy

set "K2=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%COMPY_PORT% " ^| findstr LISTENING') do (
  taskkill /F /PID %%P >nul 2>nul
  set "K2=1"
)
if "%K2%"=="0" ( echo       端口 %COMPY_PORT% 没有运行中的服务 ) else ( echo       ComfyUI 已停止 )
goto :ask_ollama

:skip_comfy
echo       已保留（端口 %COMPY_PORT%）。

rem ---------- 3) Ollama（可选） ----------
:ask_ollama
echo.
echo [3/3] Ollama 提示词扩写服务（端口 %OLLAMA_PORT%）
echo       它可能还被别的程序用着；停止后「扩写 / 维度」会退化成离线规则版。
echo       （若托盘里的 Ollama 图标还在，它可能会把服务重新拉起来。）
set "A3="
set /p A3=       要一起停止吗？输入 Y 停止，直接回车保留:
if /i not "%A3%"=="Y" goto :done

set "K3=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%OLLAMA_PORT% " ^| findstr LISTENING') do (
  taskkill /F /PID %%P >nul 2>nul
  set "K3=1"
)
if "%K3%"=="0" ( echo       端口 %OLLAMA_PORT% 没有运行中的服务 ) else ( echo       Ollama 已停止 )

:done
echo.
echo ------------------------------------------------------------
echo  收工。
echo ------------------------------------------------------------
echo.
pause
exit /b 0
