#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
出图前清理显存 —— 卸载 Ollama 驻留在显卡上的模型。

为什么需要
----------
8 GB 卡上 Ollama 和 ComfyUI 抢显存。2026-10-02 实测事故：
Ollama 驻留 qwen3.5-9b(5.40 GB) + nomic-embed-text(0.30 GB) 时，
ComfyUI 的参考图编辑档每步从 **9.7 s 涨到 123~252 s**（整张 6 步要 12 分钟）。

判据：DiT(4.2 GB) + TE(5.9 GB) 装不进剩下的显存 ⇒ 每步都在 CPU/GPU 之间换页。
症状很有辨识度 —— **GPU 利用率 100% 但功耗只有 15 W**（正常满负荷 35~44 W）。
卸载 Ollama 模型后功耗立刻回到 44 W，速度恢复。

用法
----
    python _free_vram.py              # 清理并报告前后显存
    python _free_vram.py --quiet      # 只清理，输出精简

退出码
------
    0  正常结束（含"本来就没占用"和"连不上 Ollama"—— 都不该挡住出图）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
COMFY = "http://127.0.0.1:8188"
QUIET = "--quiet" in sys.argv

# bat 里是 chcp 936（GBK 控制台），而 Python 3 默认按 UTF-8 输出 —— 中文会乱码。
# 强制跟控制台对齐；直接在 UTF-8 终端调试时加 --utf8 跳过这一步。
if "--utf8" not in sys.argv:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="gbk", errors="replace")
        except Exception:
            pass

# 企业环境有代理，必须绕过，否则连 127.0.0.1 也会被劫走
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(msg=""):
    if not QUIET:
        print(msg, flush=True)


def gpu_used():
    """返回当前显存占用 MiB；取不到返回 None。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        used, total = (r.stdout.strip().splitlines()[0]).split(",")
        return int(used.strip()), int(total.strip())
    except Exception:
        return None


def http_json(url, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_quiet(url, payload, timeout=60):
    """POST 一个 JSON，**不解析响应体**。

    ComfyUI 的 `/free` 会返回一个空 body（不是 JSON）——
    用 http_json 去解析会假报错「Expecting value: line 1 column 1」，
    但请求其实已经成功了。所以这里只看 HTTP 状态码。
    """
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as resp:
        resp.read()
        return resp.status


def comfy_free():
    """让 ComfyUI 卸载自己的缓存模型。

    返回 (是否可达, 释放前 torch 已分配 GiB, 释放后 torch 已分配 GiB)。

    ⚠️ 2026-10-04 真事故：ComfyUI 出过图之后会**一直攥着 3.9 GiB 不放**，
    而此时 `ollama ps` 是空的、GPU 利用率 0%、功耗 1.45 W —— 看起来「没人占」，
    但显存就是下不来（曾害得一次 E2E 白等 5 小时）。
    判据：`/system_stats` 里 `torch_vram_total` 不为 0 就是它攥着。
    """
    def torch_alloc():
        try:
            d = http_json(f"{COMFY}/system_stats", timeout=8)
            return d["devices"][0].get("torch_vram_total", 0) / 2 ** 30
        except Exception:
            return None

    t0 = torch_alloc()
    if t0 is None:
        return False, None, None
    try:
        http_post_quiet(f"{COMFY}/free", {"unload_models": True, "free_memory": True}, timeout=30)
    except Exception as e:
        say(f"  [失败] ComfyUI /free 调用失败：{e}")
        return True, t0, None
    time.sleep(2)
    return True, t0, torch_alloc()


def main():
    before = gpu_used()
    if before:
        say(f"清理前显存：{before[0]} / {before[1]} MiB")

    # 1) ComfyUI 自己的缓存模型（2026-10-04 补）
    reachable, t0, t1 = comfy_free()
    if not reachable:
        say("ComfyUI 没响应 —— 没在跑就不用清，继续。")
    elif t0 and t0 > 0.5:
        tail = f"，现剩 {t1:.2f} GiB" if t1 is not None else ""
        say(f"ComfyUI 缓存模型占着 {t0:.2f} GiB，已请求卸载{tail}")
    else:
        say("ComfyUI 没有缓存模型占用显存。")

    # 2) Ollama 驻留模型
    freed = 0.0
    try:
        ps = http_json(f"{OLLAMA}/api/ps", timeout=8)
    except Exception as e:
        say(f"Ollama 没响应（{type(e).__name__}）—— 没在跑就不用清理。")
        ps = None

    if ps is not None:
        on_gpu = [m for m in (ps.get("models") or []) if (m.get("size_vram") or 0) > 0]
        if not on_gpu:
            say("Ollama 没有模型占用显存，无需清理。")
        else:
            say(f"发现 {len(on_gpu)} 个 Ollama 模型占着显存，正在卸载：")
            for m in on_gpu:
                name = m.get("name") or "?"
                vram = (m.get("size_vram") or 0) / 2 ** 30
                try:
                    # keep_alive=0 让 Ollama 立刻把它从显存卸下来
                    http_json(f"{OLLAMA}/api/generate",
                              {"model": name, "keep_alive": 0}, timeout=60)
                    say(f"  [OK] {name:44s} 释放 {vram:.2f} GB")
                    freed += vram
                except Exception as e:
                    say(f"  [失败] {name:44s} 卸载失败：{e}")

    # 3) 复核
    after = gpu_used()
    if before and after:
        say(f"清理后显存：{after[0]} / {after[1]} MiB"
            f"（本进程视角释放 {before[0] - after[0]} MiB）")
    if freed:
        say(f"合计向 Ollama 要回 {freed:.2f} GB 显存。")

    # 4) 有些模型被别的服务设了永久驻留（expires_at 在很远的未来），
    #    卸载后可能立刻被拉回来 —— 复核一下，提醒用户。
    if ps is not None:
        try:
            ps2 = http_json(f"{OLLAMA}/api/ps", timeout=8)
            back = [m.get("name") for m in (ps2.get("models") or [])
                    if (m.get("size_vram") or 0) > 0]
            if back:
                say("")
                say("【注意】：这些模型卸载后又被拉回显存了：")
                for n in back:
                    say(f"     {n}")
                say("   说明有别的程序在持续调用它（比如某个 RAG / 检索服务）。")
                say("   只清一次没用 —— 要么停掉那个服务，要么在出图期间别用它。")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
