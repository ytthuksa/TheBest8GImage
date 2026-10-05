@echo off
chcp 936 >nul
title 放行出图台端口（局域网访问用）
setlocal
set "WEB_PORT=8190"
set "RULE=Qwen21 出图台"
set "LOG=%~dp0放行出图台端口.log"

rem ---- 检查管理员权限 ----
net session >nul 2>&1
if errorlevel 1 goto :need_admin

echo ============================================================
echo    放行出图台端口 %WEB_PORT%（仅限局域网）
echo ============================================================
echo.
echo 写入日志：%LOG%
echo 放行出图台端口 %WEB_PORT%  >> "%LOG%"

rem ---- 先删旧规则，避免重复 ----
netsh advfirewall firewall delete rule name="%RULE%" >> "%LOG%" 2>&1

rem ---- 添加规则：只允许同一局域网（remoteip=LocalSubnet） ----
netsh advfirewall firewall add rule name="%RULE%" ^
    dir=in action=allow protocol=TCP localport=%WEB_PORT% ^
    remoteip=LocalSubnet profile=any >> "%LOG%" 2>&1

echo 当前规则： >> "%LOG%"
netsh advfirewall firewall show rule name="%RULE%" >> "%LOG%" 2>&1

echo ---- 结果 ----
netsh advfirewall firewall show rule name="%RULE%" | findstr /i "规则名称 Rule 已启用 Enabled 本地端口 LocalPort 远程IP RemoteIP 操作 Action"
echo.
echo ------------------------------------------------------------
echo  完成。现在双击「启动出图台-局域网.bat」，手机连同一个 WiFi
echo  打开 http://本机IP:%WEB_PORT% 即可。
echo  要撤销：netsh advfirewall firewall delete rule name="%RULE%"
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:need_admin
echo ============================================================
echo   需要管理员权限
echo ============================================================
echo.
echo  请关掉这个窗口，在文件上【右键】-【以管理员身份运行】。
echo.
pause
exit /b 1
