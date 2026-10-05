# -*- coding: utf-8 -*-
"""
把「端口体检」（_zombie_check.py）接进两个启动脚本。**幂等**，重复跑不会重复插。

为什么必须用这个脚本改、不能直接编辑 .bat：
    Windows 中文系统的 .bat 必须是 **GBK + CRLF**。用普通文本编辑器保存会变成
    UTF-8，双击就一片乱码。这里强制 GBK + CRLF 写回。

改哪两个：
    启动出图台.bat  —— 把「netstat 看端口在不在」换成「HTTP 体检」，
                        并加一个 :comfy_stuck 分支给出人话的处置办法。
    启动ComfyUI.bat —— 在开浏览器**之前**插入体检（顺序很关键：
                        僵死端口 TCP 也能连上，浏览器会打开一个连不上的页面）。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "_zombie_check.py"

# ------------------------------------------------------------------ 出图台
WEB_OLD = (
    "rem ---------- 3) ComfyUI 推理后端 ----------\r\n"
    ":comfy_check\r\n"
    'netstat -ano | findstr ":%COMPY_PORT% " | findstr LISTENING >nul\r\n'
    "if not errorlevel 1 goto :comfy_ok\r\n"
    "echo [3/5] 启动 ComfyUI 推理后端（首次加载模型约 30 秒）...\r\n"
)
WEB_NEW = (
    "rem ---------- 3) ComfyUI 推理后端 ----------\r\n"
    ":comfy_check\r\n"
    "rem 端口在监听 != 服务活着。僵死进程会永久挡住启动（2026-10-02 事故：点了三次都起不来）\r\n"
    '"%PY%" _zombie_check.py %COMPY_PORT% --name ComfyUI --proc python.exe\r\n'
    "if errorlevel 3 goto :comfy_ok\r\n"
    "if errorlevel 1 goto :comfy_stuck\r\n"
    "echo [3/5] 启动 ComfyUI 推理后端（首次加载模型约 30 秒）...\r\n"
)

WEB_ANCHOR = "rem ---------- 4) 网页服务 ----------\r\n"
WEB_INSERT = (
    ":comfy_stuck\r\n"
    "echo       [失败] 端口 %COMPY_PORT% 被一个清不掉的僵死进程占着，ComfyUI 起不来。\r\n"
    "echo              处理办法：Ctrl+Shift+Esc 打开任务管理器 - 详细信息 - 结束所有 python.exe，\r\n"
    "echo              然后重新双击本脚本。还不行就重启电脑（最彻底）。\r\n"
    "echo.\r\n"
    "echo              现在仍会打开网页，但顶部的「ComfyUI 在线」指示灯会是红的。\r\n"
    "goto :start_web\r\n"
    "\r\n"
)

# ------------------------------------------------------------------ 纯 ComfyUI
CFY_ANCHOR = (
    "rem Poll the TCP port in a hidden PowerShell, then open the default browser.\r\n"
)
CFY_INSERT = (
    "rem ---- 启动前端口体检：清掉可能存在的僵死进程 ----\r\n"
    "rem 顺序很关键：必须在开浏览器之前。僵死端口的 TCP 也能连上，\r\n"
    "rem 不先清理的话浏览器会打开一个永远转圈、连不上后端的页面。\r\n"
    '"%PY%" _zombie_check.py %PORT% --name ComfyUI --proc python.exe\r\n'
    "if errorlevel 3 goto :already_running\r\n"
    "if errorlevel 1 goto :stuck\r\n"
    "echo.\r\n"
    "\r\n"
)

CFY_TAIL_OLD = (
    "echo.\r\n"
    "echo [ComfyUI exited]\r\n"
    "pause\r\n"
)
CFY_TAIL_NEW = (
    "echo.\r\n"
    "echo [ComfyUI exited]\r\n"
    "pause\r\n"
    "exit /b 0\r\n"
    "\r\n"
    ":already_running\r\n"
    "echo.\r\n"
    "echo ============================================================\r\n"
    "echo   ComfyUI 已经在运行了，不用重复启动。\r\n"
    "echo   直接打开：http://127.0.0.1:%PORT%/\r\n"
    "echo ============================================================\r\n"
    'start "" http://127.0.0.1:%PORT%/\r\n'
    "pause\r\n"
    "exit /b 0\r\n"
    "\r\n"
    ":stuck\r\n"
    "echo.\r\n"
    "echo ============================================================\r\n"
    "echo   [失败] 端口 %PORT% 被一个清不掉的僵死进程占着，ComfyUI 起不来。\r\n"
    "echo.\r\n"
    "echo   处理办法（任选）：\r\n"
    "echo     1. Ctrl+Shift+Esc 打开任务管理器 - 详细信息 - 结束所有 python.exe，再重跑\r\n"
    "echo     2. 重启电脑（最彻底）\r\n"
    "echo ============================================================\r\n"
    "pause\r\n"
    "exit /b 1\r\n"
)


def read_bat(path):
    with open(path, "rb") as fh:
        return fh.read().decode("gbk")


def write_bat(path, text):
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")   # 强制 CRLF
    with open(path, "wb") as fh:
        fh.write(text.encode("gbk"))
    raw = open(path, "rb").read()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n")
    print("    写回 %s : %d 字节  CRLF=%d  裸LF=%d"
          % (os.path.basename(path), len(raw), crlf, lf - crlf))


def patch_web(path):
    t = read_bat(path)
    if MARKER in t:
        print("  [跳过] %s 已接好体检（幂等）。" % os.path.basename(path))
        return False
    if WEB_OLD not in t:
        print("  [失败] %s 里找不到要替换的 :comfy_check 块，没动它。" % os.path.basename(path))
        return False
    if WEB_ANCHOR not in t:
        print("  [失败] %s 里找不到「网页服务」锚点，没动它。" % os.path.basename(path))
        return False
    t = t.replace(WEB_OLD, WEB_NEW, 1)
    t = t.replace(WEB_ANCHOR, WEB_INSERT + WEB_ANCHOR, 1)
    write_bat(path, t)
    print("  [OK] %s 已接入体检 + :comfy_stuck 分支。" % os.path.basename(path))
    return True


def patch_comfy(path):
    t = read_bat(path)
    if MARKER in t:
        print("  [跳过] %s 已接好体检（幂等）。" % os.path.basename(path))
        return False
    if CFY_ANCHOR not in t:
        print("  [失败] %s 里找不到浏览器轮询锚点，没动它。" % os.path.basename(path))
        return False
    if CFY_TAIL_OLD not in t:
        print("  [失败] %s 里找不到结尾 [ComfyUI exited] 块，没动它。" % os.path.basename(path))
        return False
    t = t.replace(CFY_ANCHOR, CFY_INSERT + CFY_ANCHOR, 1)
    t = t.replace(CFY_TAIL_OLD, CFY_TAIL_NEW, 1)
    write_bat(path, t)
    print("  [OK] %s 已接入体检 + :already_running / :stuck 分支。" % os.path.basename(path))
    return True


def main():
    if "--utf8" not in sys.argv:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass

    print("=" * 62)
    print("  给启动脚本接入「端口体检」（僵尸进程自动清理）")
    print("=" * 62)
    if not os.path.isfile(os.path.join(HERE, "_zombie_check.py")):
        print("  [失败] 找不到 _zombie_check.py，先把它放好再跑。")
        sys.exit(1)
    patch_web(os.path.join(HERE, "启动出图台.bat"))
    patch_comfy(os.path.join(HERE, "启动ComfyUI.bat"))
    print("-" * 62)
    print("  完成。现在双击启动脚本时，会先做 HTTP 体检：")
    print("    健康    -> 跳过启动，直接开网页")
    print("    空闲    -> 正常启动")
    print("    僵死    -> 自动杀掉（连父进程一起），然后正常启动")
    print("    杀不掉  -> 明确告诉用户去任务管理器 / 重启")


if __name__ == "__main__":
    main()
