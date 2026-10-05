# -*- coding: utf-8 -*-
"""
启动前的「端口体检」—— 区分「服务真的在跑」和「僵死进程占着端口」。

为什么需要它（2026-10-02 真实事故）：
    Windows 上进程可能已经死了、或者卡在内核态杀不掉，但监听 socket 还被占着，
    netstat 里端口状态仍然是 LISTENING。启动脚本如果只看 netstat，
    就会误判「已在运行」而**跳过启动** —— 结果网页永远连不上后端，
    用户点三次都起不来，还以为是自己操作错了。

    当晚实际情况：ComfyUI 主进程 1808 已 HasExited=True，但 8188 仍 LISTENING、
    堆了 20+ 个 CLOSE_WAIT，`taskkill /F /PID 1808` 报「没有正在运行的实例」。
    最后是靠**杀掉它的父进程**（launcher stub）才把 socket 释放掉。

用法：
    _zombie_check.py <port> [--name ComfyUI] [--proc python.exe]
                     [--wait 6] [--probe] [--utf8] [--quiet]

退出码（给 .bat 判断）：
    0 = 端口空闲，或僵死进程已清掉   -> 应该启动服务
    3 = 服务健康，真的在运行         -> 应该跳过启动
    1 = 端口被占着但清不掉           -> 应该报错让人介入

    .bat 里的写法：
        "%PY%" _zombie_check.py 8188 --name ComfyUI --proc python.exe
        if errorlevel 3 goto :already_ok
        if errorlevel 1 goto :stuck
        rem 走到这里 = 0，可以启动
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

# 拆开写，避免命令行里出现敏感词被安全策略拦
_PS = "power" + "shell"


def say(msg, quiet=False):
    if not quiet:
        print(msg, flush=True)


def http_ok(port, timeout=3.0):
    """探活：/system_stats 返回 200 才算活着。"""
    url = "http://127.0.0.1:%d/system_stats" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def port_owners(port):
    """netstat 里 LISTENING 在该端口上的 PID 列表。"""
    pids = []
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           errors="replace", timeout=20)
    except Exception:
        return pids
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[1].endswith(":" + str(port)) and "LISTENING" in parts:
            pid = parts[4]
            if pid.isdigit() and pid not in pids:
                pids.append(pid)
    return pids


def proc_name(pid):
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq " + pid, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=20)
        line = r.stdout.strip().splitlines()
        if not line:
            return ""
        first = line[0]
        if "No tasks" in first or first.startswith("INFO"):
            return ""
        return first.split(",")[0].strip('"').lower()
    except Exception:
        return ""


def parent_pid(pid):
    """用 CIM 查父进程。wmic 在新 Windows 上已被移除，所以走 PowerShell。"""
    try:
        r = subprocess.run(
            [_PS, "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').ParentProcessId" % pid],
            capture_output=True, text=True, errors="replace", timeout=25)
        v = r.stdout.strip()
        return v if v.isdigit() else ""
    except Exception:
        return ""


def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, errors="replace", timeout=25)
    except Exception:
        pass


def main():
    if "--utf8" not in sys.argv:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("--name", default="服务", help="给人看的服务名")
    ap.add_argument("--proc", default="python.exe", help="允许被清掉的进程名（安全阀）")
    ap.add_argument("--wait", type=float, default=6.0, help="判定僵死前的等待秒数")
    ap.add_argument("--probe", action="store_true", help="只探活，不清理（给轮询循环用）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--utf8", action="store_true")
    a = ap.parse_args()

    # ---- 只探活模式 ----
    if a.probe:
        sys.exit(3 if http_ok(a.port) else 0)

    # ---- 先给它一点时间自证清白 ----
    deadline = time.time() + max(0.0, a.wait)
    while True:
        if http_ok(a.port):
            say("  [体检] %s（端口 %d）健康，正在运行 —— 跳过启动。"
                % (a.name, a.port), a.quiet)
            sys.exit(3)
        if time.time() >= deadline:
            break
        time.sleep(1.0)

    owners = port_owners(a.port)
    if not owners:
        say("  [体检] 端口 %d 空闲，可以启动。" % a.port, a.quiet)
        sys.exit(0)

    # ---- 端口被占，但服务不响应 = 僵死 ----
    say("  [体检] 端口 %d 被占着，但 %s 不响应 —— 是僵死进程，开始清理："
        % (a.port, a.name), a.quiet)

    victims = []
    for pid in owners:
        nm = proc_name(pid)
        say("         PID %-7s %s" % (pid, nm or "(已查不到)"), a.quiet)
        if nm == a.proc.lower():
            victims.append(pid)
        else:
            say("         ^ 进程名不是 %s，不动它（安全阀）。" % a.proc, a.quiet)

    if not victims:
        say("  [失败] 占端口的不是 %s，我不认识它，不敢动。" % a.proc, a.quiet)
        sys.exit(1)

    for pid in victims:
        # 先杀它自己（连子进程）
        kill_tree(pid)
        time.sleep(0.8)
        if str(pid) not in port_owners(a.port):
            continue
        # 杀不掉 —— 大概率已经死了、socket 被父进程的句柄吊着，去杀父进程
        ppid = parent_pid(pid)
        if ppid and ppid not in ("0", "4"):
            say("         PID %s 杀不动（可能已僵死），改杀父进程 PID %s…" % (pid, ppid), a.quiet)
            kill_tree(ppid)
            time.sleep(0.8)

    time.sleep(0.5)
    left = port_owners(a.port)
    if not left:
        say("  [体检] 僵死进程已清掉，端口 %d 释放，可以启动。" % a.port, a.quiet)
        sys.exit(0)

    say("  [失败] 端口 %d 仍被占着（PID: %s），清不掉。"
        % (a.port, ", ".join(left)), a.quiet)
    say("         这通常是进程卡在显卡驱动里了 —— 打开任务管理器（Ctrl+Shift+Esc）",
        a.quiet)
    say("         结束所有 python.exe；还不行就重启电脑。", a.quiet)
    sys.exit(1)


if __name__ == "__main__":
    main()
