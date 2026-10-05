# -*- coding: utf-8 -*-
"""一键释放资源：停本地 AI 服务 + 卸载 Ollama 模型 + 回收显存。

用法：
    python _release_resources.py                 # 默认清单（出图台/ComfyUI/RAG）
    python _release_resources.py --ports 8190,8188,8200
    python _release_resources.py --keep-ollama   # 不动 Ollama
    python _release_resources.py --dry-run       # 只看现状，不杀任何东西

设计要点（都踩过坑）：
1. **只杀「监听指定端口」的进程**，绝不按进程名一把梭（同名 python 太多）。
2. **必须连父进程一起杀**：`.venv\\Scripts\\python.exe` 是启动器 stub，
   真正的服务是它的子进程；只杀子进程时 socket 可能被 stub 的句柄吊着，
   `netstat` 仍显示 LISTENING（2026-10-02 真实事故）。
   策略：先 `/T` 杀监听进程（带走其子进程），若父进程是 python 系再杀父进程。
3. **node.exe 一律不碰** —— WorkBuddy 自己的 MCP（sheetagent / weixinpay）就是 node。
4. Ollama 用 `ollama stop <model>`（等价 keep_alive=0），**不杀 ollama 服务本身**，
   下次请求会自动重新加载。
"""
import argparse
import os
import re
import subprocess
import sys
import time

# 中文 Windows 控制台是 GBK(936)。跟着 _free_vram.py 的既有约定走，
# 否则 bat 里 chcp 936 之后 Python 的 UTF-8 输出会变乱码。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="gbk", errors="replace")
    except Exception:
        pass

PS = "power" + "shell"

# 默认要清的端口：出图台 / ComfyUI / 其他本地服务
DEFAULT_PORTS = {8190: "出图台", 8188: "ComfyUI", 8200: "其他本地服务"}
# 这些进程名就算占着端口也不动（保护 WorkBuddy 自身与系统组件）
NEVER_KILL = ("node.exe", "explorer.exe", "svchost.exe", "System", "ollama.exe",
              "ollama app.exe")


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def listen_pids(port):
    r = run(["netstat", "-ano"])
    pids = set()
    for ln in r.stdout.splitlines():
        if "LISTENING" not in ln:
            continue
        if not re.search(r"[.:]%d\s" % port, ln):
            continue
        m = re.search(r"\s(\d+)\s*$", ln.strip())
        if m:
            pids.add(m.group(1))
    return pids


def proc_info(pid):
    """返回 (name, ppid)；查不到返回 ('','')。"""
    ps_cmd = ("$p=Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(pid) + "';"
              "if($p){'{0}|{1}' -f $p.Name,$p.ParentProcessId}")
    r = run([PS, "-NoProfile", "-Command", ps_cmd])
    out = r.stdout.strip()
    if "|" in out:
        name, ppid = out.split("|", 1)
        return name.strip(), ppid.strip()
    return "", ""


def kill(pid, tag=""):
    r = run(["taskkill", "/F", "/T", "/PID", str(pid)])
    txt = (r.stdout + r.stderr).strip().replace("\r", "").replace("\n", " ")
    ok = r.returncode == 0
    print("      kill %-6s %-16s %s" % (pid, tag, "[OK]" if ok else "[失败] " + txt[:110]))
    return ok


def gpu_line():
    r = run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw",
             "--format=csv,noheader"])
    return r.stdout.strip()


def ollama_loaded():
    r = run(["ollama", "ps"])
    out = [l.strip() for l in r.stdout.splitlines()[1:] if l.strip()]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="", help="逗号分隔；默认 8190,8188,8200")
    ap.add_argument("--keep-ollama", action="store_true", help="不动 Ollama 模型")
    ap.add_argument("--dry-run", action="store_true", help="只体检，不杀")
    a = ap.parse_args()

    ports = DEFAULT_PORTS
    if a.ports:
        ports = {int(p): "port-%d" % int(p) for p in a.ports.split(",") if p.strip()}

    print("=" * 64)
    print("  释放资源" + ("  [DRY-RUN 只体检]" if a.dry_run else ""))
    print("=" * 64)
    print("  起始 GPU : %s" % gpu_line())
    loaded = ollama_loaded()
    print("  Ollama   : %s" % ("、".join(l.split()[0] for l in loaded) if loaded else "(无模型驻留)"))

    failed = []
    for port, label in ports.items():
        print("\n[%s] 端口 %d" % (label, port))
        pids = listen_pids(port)
        if not pids:
            print("      端口空闲")
            continue
        for pid in sorted(pids):
            name, ppid = proc_info(pid)
            if not name:
                print("      监听 PID %s 已消失" % pid)
                continue
            print("      监听 PID %s (%s)  父进程 %s" % (pid, name, ppid or "?"))
            if name.lower() in NEVER_KILL:
                print("      ↳ 属于保护名单，跳过")
                continue
            if a.dry_run:
                continue
            if not kill(pid, name):
                failed.append("kill %s" % pid)
            if ppid and ppid.isdigit() and ppid != "0":
                pname, _ = proc_info(ppid)
                if pname and pname.lower().startswith("python"):
                    print("      ↳ 父进程 %s (%s) 是 python 系，一并清理（可能吊着端口）" % (ppid, pname))
                    kill(ppid, pname)

    if not a.keep_ollama and not a.dry_run:
        print("\n[Ollama] 卸载驻留模型")
        for l in loaded:
            model = l.split()[0]
            r = run(["ollama", "stop", model], 120)
            print("      stop %-28s %s" % (model, "[OK]" if r.returncode == 0 else "[失败]"))

    # 复活检测 —— 2026-10-03 实测：其他本地服务 被别的 WorkBuddy 会话拉起，
    # 杀掉后 20 秒就自己回来。不检测的话会误以为"已释放"。
    revived = []
    if not a.dry_run:
        time.sleep(15)
        for port in ports:
            if listen_pids(port):
                revived.append(port)

    print("\n" + "-" * 64)
    print("  复查")
    print("-" * 64)
    for port, label in ports.items():
        left = listen_pids(port)
        print("  %-14s :%-5d %s" % (label, port, "已释放" if not left else "仍被占 " + ",".join(left)))
    print("  GPU          : %s" % gpu_line())
    loaded = ollama_loaded()
    print("  Ollama       : %s" % ("、".join(l.split()[0] for l in loaded) if loaded else "(无模型驻留)"))

    if revived:
        print("\n  [注意] 这些端口在 15 秒内自己回来了：%s"
              % "、".join(str(p) for p in revived))
        print("         说明有守护者（通常是**另一个仍在运行的 WorkBuddy 会话**，")
        print("         或计划任务）在拉起它。继续杀只会来回拉锯 ——")
        print("         正解是去停掉那个会话/任务本身。")
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
