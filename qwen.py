#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qwen.py —— Qwen-Image-2.1 本地出图命令行入口（给 AI / 脚本调用）

复用 `_server.py` 里已经端到端验证过的逻辑（同一套配方、同一个注入路径），
只是换成命令行门面。**只依赖标准库**，托管 Python 也能跑。

用法
----
    python qwen.py t2i "一只橘猫坐在窗台上" -o cat.png
    python qwen.py edit "把背景换成雨夜霓虹东京街头" -r photo.png
    python qwen.py bgremove -r photo.png
    python qwen.py t2i "一只橘猫" --lora turbo          # 加速档（默认 6 步，约 2x 快）
    python qwen.py t2i "霓虹招牌写着 OPEN" --lora turbo --turbo-steps 8   # 密集文字档
    python qwen.py edit "换成夜景" -r photo.png --lora turbo
    python qwen.py edit "把外套换成红色风衣" -r photo.png --lora turbo --lora2 consistency
    python qwen.py t2i "一只橘猫" --lora turbo --vae texture   # 换 VAE 解码器
    python qwen.py t2i "浴室里的女人" --boldness L2        # 大胆度档位：自动补机位+姿势
    python qwen.py enhance "赛博朋克猫"
    python qwen.py loras
    python qwen.py vaes
    python qwen.py status
    python qwen.py list

通用选项
--------
    --seed N        随机种子（-1 = 随机，默认）
    --steps N       采样步数（默认按档位配方；加速档下不生效，改用 --turbo-steps）
    --cfg N         CFG 引导强度（默认按档位配方；加速档不生效）
    --size WxH      仅 t2i：输出尺寸，如 1024x1024 / 1280（默认 1024x1024）
    --res N         仅 edit/bgremove：参考图缩放长边（默认 1024）
    --negative "…"  负向提示词（加速档不生效）
    --sampler NAME  采样器（默认 res_multistep；加速档固定 euler）
    --scheduler N   调度器（默认 simple；加速档用 viggle sigma 表）
    --lora NAME     LoRA 文件名，或 'turbo' 简写（自动挑 viggle-turbo 加速 LoRA）；
                    支持模糊匹配（'consistency' → 完整文件名）
    --lora-weight N LoRA 权重（默认 1.0）
    --lora2 NAME    附加 LoRA（第二槽位，与主槽位**串联叠加**）；只接受普通 LoRA
                    （加速档 LoRA 必须放 --lora）。例：--lora turbo --lora2 consistency
    --lora2-weight N 附加 LoRA 权重（默认 1.0）
    --vae NAME      切换 VAE 解码器：'official'（默认）/ 'texture'（texture-fix）/ 文件名
                    用 `qwen.py vaes` 看列表。对全档生效（含局部重绘）
    --boldness L2   大胆度档位（L0/L1/L2/L3）：只把「机位 + 姿势」追加到提示词末尾，
                    不动任何采样参数。仅 t2i / edit 生效（抠图、姿势档忽略）
    --turbo / --no-turbo
                    强制开/关加速档；不指定则按 LoRA 类型自动（加速 LoRA 默认开）
    --turbo-steps N 加速档步数预设（官方 sigma 表，只有 4/5/6/7/8 合法）
                    6 = 默认推荐；8 = 画面里有小字时用；4/5 = 旧版档
    -o, --out PATH  把产物复制到该路径
    --timeout N     等待上限秒数（默认 900）
    --dry-run       只打印将提交的工作流，不真的提交
    --json          机器可读输出（最后一行是 JSON）
    --quiet         不打印进度

退出码：0 成功 / 2 参数错 / 3 ComfyUI 不可用 / 4 生成失败 / 5 超时
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import _server as S  # noqa: E402  复用已验证的配方与提交逻辑

INPUT_DIR = os.path.join(BASE, "input")
OUTPUT_DIR = os.path.join(BASE, "output")

EXIT_OK, EXIT_ARGS, EXIT_NOCOMFY, EXIT_FAIL, EXIT_TIMEOUT = 0, 2, 3, 4, 5


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def log(msg, quiet=False):
    if not quiet:
        print(msg, flush=True)


def warn(msg, quiet=False):
    """提示信息走 stderr —— enhance 的 stdout 是提示词本体，不能被污染。"""
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def gpu_busy():
    """粗略判断独显是否被别的进程占着（只提示，不阻断）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8).stdout.strip()
        if not out:
            return None
        used, total, util = [int(x) for x in out.splitlines()[0].split(",")]
        return used, total, util
    except Exception:
        return None


def stage_ref(path):
    """把参考图拷进 ComfyUI 的 input/ 目录，返回 LoadImage 能用的文件名。"""
    if not path:
        raise ValueError("这个模式需要参考图（-r/--ref）")
    if not os.path.isfile(path):
        raise ValueError(f"参考图不存在: {path}")
    os.makedirs(INPUT_DIR, exist_ok=True)
    name = os.path.basename(path)
    dst = os.path.join(INPUT_DIR, name)
    if os.path.abspath(path) != os.path.abspath(dst):
        shutil.copy2(path, dst)
    return name


def parse_size(s, default=1024):
    if not s:
        return default, default
    s = str(s).lower().replace("*", "x")
    if "x" in s:
        w, h = s.split("x", 1)
        return int(w), int(h)
    return int(s), int(s)


def find_output(im):
    return os.path.join(OUTPUT_DIR, im.get("subfolder") or "", im["filename"])


def copy_many(srcs, out_path):
    """
    把 N 张产物复制到 out_path；多张时自动加 _2 / _3 后缀，**不能互相覆盖**。
    返回复制后的路径列表（顺序与 srcs 一致）。
    """
    base, ext = os.path.splitext(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    res = []
    for i, s in enumerate(srcs):
        dst = out_path if i == 0 else f"{base}_{i + 1}{ext}"
        shutil.copy2(s, dst)
        res.append(dst)
    return res


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------
def default_turbo_lora():
    """挑一个 viggle-turbo 加速 LoRA：优先官方推荐的 v0.2.1 r128。"""
    avail = [x["file"] for x in S.list_loras() if x.get("turbo")]
    if not avail:
        return None
    for fn in avail:
        if "v0.2.1" in fn and "r128" in fn:
            return fn
    return avail[0]


def resolve_lora(name):
    """LoRA 简写/模糊名 → 真实文件名。

    先精确匹配，再按「去掉 .safetensors 后子串匹配」兜底 —— 免得每次都要敲一长串文件名。
    例如 'consistency' → 'qwen-image-2.1-consistency.safetensors'。
    """
    name = (name or "").strip()
    if not name:
        return None
    files = [x["file"] for x in S.list_loras()]
    if name in files:
        return name
    stem = name[:-len(".safetensors")] if name.endswith(".safetensors") else name
    hits = [f for f in files if stem.lower() in f.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # 多个命中时优先取最短的（通常是通用版而不是某个变体）
        return sorted(hits, key=len)[0]
    return None


def resolve_vae(name):
    """VAE 简写 → 真实文件名。'official' = 模板自带（返回空串表示不切换）。"""
    name = (name or "").strip()
    if not name:
        return None
    low = name.lower()
    if low in ("official", "官方", "default", "none", "-"):
        return ""          # 空值 = 用模板自带，_apply_vae 会直接跳过
    files = [x["file"] for x in S.list_vaes()]
    if name in files:
        return name
    if low in ("texture", "texture-fix", "texture_fix", "fix"):
        for f in files:
            if "texture_fix" in f:
                return f
    hits = [f for f in files if low in f.lower()]
    return hits[0] if hits else None


def cmd_list(args):
    print("可用模式：")
    for k, r in S.RECIPES.items():
        print(f"  {k:<10} {r['label']:<12} cfg={r['cfg']}  steps={r['steps']}  "
              f"sampler={r['sampler']}/{r['scheduler']}")
    _pe = S.PE.pe_available(force=True)
    print("\n提示词扩写模型：",
          ("官方 PE-T2I · " + S.PE.PE_MODEL) if _pe
          else ("通用 · " + S.PE.FALLBACK_MODEL))
    if not _pe:
        print("  （想用官方 PE：ollama create %s -f models\\prompt_enhancer\\Modelfile）"
              % S.PE.PE_MODEL)
    return EXIT_OK


def cmd_loras(args):
    ls = S.list_loras()
    if not ls:
        print("models/loras 下没有 LoRA 文件")
        return EXIT_OK
    print("可用 LoRA：")
    for x in ls:
        tag = "加速" if x.get("turbo") else "普通"
        print(f"  [{tag}] {x['file']}  ({x['size_mb']} MB)")
        if x.get("label") and x["label"] != x["file"]:
            print(f"         {x['label']}" + (f" —— {x['note']}" if x.get("note") else ""))
    t = default_turbo_lora()
    if t:
        print("\n加速档步数预设（官方 raw sigma 表，用 --turbo-steps N 选）：")
        for n in sorted(S.TURBO_SCHEDULES):
            nodes, label, note = S.TURBO_SCHEDULES[n]
            mark = " ← 默认" if n == 6 else ""
            print(f"  {n} 步  {label}{mark}")
            print(f"         sigmas=[{nodes}]")
            if note:
                print(f"         {note}")
        print(f"\n用法：--lora turbo（= {t}）[--turbo-steps 8]\n"
              f"  实测 1024×1024 文生图 63.1s → 30.1s（2.10x），参考图编辑 ~186s → 58s")
    print("\n提示：LoRA 有两个槽位 —— 主槽位 --lora（可放加速档），附加槽位 --lora2（只放普通 LoRA，"
          "与主槽位串联叠加）。\n      qwen.py vaes 可以看可切换的 VAE。")
    return EXIT_OK


def cmd_vaes(args):
    vs = S.list_vaes()
    if not vs:
        print("models/vae 下没有可切换的 VAE 文件")
        return EXIT_OK
    print("可切换的 VAE（--vae 用文件名或下面的简写）：")
    for x in vs:
        short = ""
        if "texture_fix" in x["file"]:
            short = "  简写：texture"
        elif x["file"] == S.DEFAULT_VAE:
            short = "  简写：official（模板自带，等同不传 --vae）"
        print(f"  {x['file']}  ({x['size_mb']} MB){short}")
        if x.get("label") and x["label"] != x["file"]:
            print(f"         {x['label']}")
    print("\n注意：本机 VAE 走 bf16，官方 VAE 在 fp16 下才有的「透明区 NaN」问题不会触发；\n"
          "      texture-fix 实测与官方差异很小（均差 2/255），不建议作为默认。")
    return EXIT_OK


def cmd_styles(args):
    """列出风格预设（纯提示词，不需要 LoRA）。"""
    ls = S.style_list()
    if not ls:
        print("没有风格预设")
        return EXIT_OK
    show_nsfw = getattr(args, "nsfw", False)
    print("风格预设（纯提示词块，不需要 LoRA —— 加在提示词最前面）：\n")
    n = 0
    for x in ls:
        if x["nsfw"] and not show_nsfw:
            continue
        n += 1
        print(f"  {x['key']:<12} {x['label']}" + ("  [成人向]" if x["nsfw"] else ""))
        if x.get("tip"):
            print(f"               {x['tip']}")
    hidden = sum(1 for x in ls if x["nsfw"]) if not show_nsfw else 0
    if hidden:
        print(f"\n（{hidden} 条成人向风格已隐藏，加 --nsfw 显示）")
    print(f"\n用法：qwen.py t2i \"一只猫坐在窗台上\" --style film")
    print(f"      qwen.py t2i \"...\" --style anime --lora turbo        # 风格 + 加速档可叠加")
    print(f"      qwen.py edit \"换成夜景\" -r in.png --style cinematic")
    print("\n⚠️ 风格块加在提示词最前面，且会覆盖「不选风格」的状态；"
          "抠图档不吃风格。")
    print("⚠️ 实测命中率 4 风格 × 3 种子 = 100%（加速档下同样成立）。"
          "详见 C:\\ComfyUI\\_styletest\\验证报告.md")
    return EXIT_OK


def cmd_status(args):
    comfy = S.comfy_up()
    st, _ = S.http_json(S.OLLAMA + "/api/tags", timeout=3)
    print(f"ComfyUI  {S.COMFY}  {'在线' if comfy else '未响应'}")
    print(f"Ollama   {S.OLLAMA}  {'在线' if st == 200 else '未启动'}")
    if st == 200:
        pe = S.PE.pe_available(force=True)
        print(f"扩写模型 {'官方 PE-T2I · ' + S.PE.PE_MODEL if pe else '通用 · ' + S.PE.FALLBACK_MODEL}"
              + ("  [纯 CPU]" if S.PE.PE_NUM_GPU == 0 else "  [GPU]"))
    g = gpu_busy()
    if g:
        used, total, util = g
        print(f"独显     {used} MiB / {total} MiB  利用率 {util}%"
              + ("   ⚠️ 正被其它进程占用" if util > 60 else ""))
    q = S.http_json(S.COMFY + "/queue", timeout=6)[1]
    if isinstance(q, dict):
        print(f"队列     运行 {len(q.get('queue_running') or [])} / "
              f"等待 {len(q.get('queue_pending') or [])}")
    return EXIT_OK if comfy else EXIT_NOCOMFY


def cmd_enhance(args):
    text = args.prompt
    if not text:
        print("需要给一个提示词", file=sys.stderr)
        return EXIT_ARGS
    count = max(1, min(3, getattr(args, "count", 1) or 1))
    lang = getattr(args, "lang", "en") or "en"
    note = None
    try:
        out, meta = S.PE.enhance(text, "t2i", count=count, lang=lang)
        src = meta.get("source")
        note = meta.get("note")
        if meta.get("wh_ratio"):
            warn(f"（建议画幅 {meta['wh_ratio']}）", args.quiet)
    except Exception as e:
        out, src = S.PE.offline_enhance(text, "t2i", count), "offline"
        warn(f"（Ollama 不可用: {e}，已用离线结构增强）", args.quiet)
    if args.json:
        print(json.dumps({"prompts": out, "source": src, "note": note},
                         ensure_ascii=False))
    else:
        for r in out:
            if count > 1:
                print(f"--- {r['label']} ---")
            print(r["text"])
            if count > 1:
                print()
    if note:
        # 段数不足的说明（模型没遵守 --- 分隔符）—— 必须让用户看到
        warn(f"（{note}）", args.quiet)
    return EXIT_OK


def cmd_generate(args, mode):
    # ---- 组参数 ----
    p = {"mode": mode, "prompt": args.prompt or "", "negative": args.negative or "",
         "filename_prefix": f"Qwen21_cli_{mode}"}
    if args.seed is not None:
        p["seed"] = args.seed
    if args.steps is not None:
        p["steps"] = args.steps
    if args.cfg is not None:
        p["cfg"] = args.cfg
    if args.sampler:
        p["sampler"] = args.sampler
    if args.scheduler:
        p["scheduler"] = args.scheduler
    if getattr(args, "batch", 1) and args.batch > 1:
        p["count"] = args.batch
    # 风格预设（纯提示词，不需要 LoRA）。后端会把它加在提示词**最前面**。
    # 抠图档不吃风格（后端会忽略）—— 这里也提一句，别让用户以为生效了。
    style = getattr(args, "style", None)
    if style:
        p["style"] = style
        if mode == "bgremove":
            warn("抠图档不吃风格预设（它的提示词是固定的抠图指令），--style 被忽略", args.quiet)
    # 大胆度档位（纯提示词）：后端把「机位 + 姿势」追加到提示词末尾，不动任何采样参数。
    # 抠图档不吃（提示词是固定指令）；姿势档也不吃（姿势由 3D 人偶 + LoRA 固定，加档位会打架）。
    boldness = getattr(args, "boldness", None)
    if boldness:
        if mode == "bgremove":
            warn("抠图档不吃大胆度档位（它的提示词是固定的抠图指令），--boldness 被忽略", args.quiet)
        elif mode == "pose":
            warn("姿势档不吃大胆度档位（姿势由 3D 人偶固定），--boldness 被忽略", args.quiet)
        else:
            p["boldness"] = boldness.strip().upper()
    lora = getattr(args, "lora", None)
    if lora and lora.strip().lower() in ("turbo", "viggle", "viggle-turbo"):
        lora = default_turbo_lora()
        if not lora:
            print("参数错误: models/loras 下找不到 viggle-turbo LoRA", file=sys.stderr)
            return EXIT_ARGS
    if lora:
        p["lora"] = lora
        p["lora_weight"] = args.lora_weight
        # --turbo / --no-turbo 三态：None = 按 LoRA 类型自动（加速 LoRA 默认开）
        if args.turbo is not None:
            p["turbo"] = args.turbo
        ts = getattr(args, "turbo_steps", None)
        if ts is not None:
            p["turbo_steps"] = ts
        # 加速档下步数由官方 sigma 表决定，--steps 会被后端忽略 —— 别让用户以为生效了
        _is_turbo_lora = any(x["file"] == lora and x.get("turbo") for x in S.list_loras())
        if _is_turbo_lora and args.turbo is not False and args.steps is not None:
            warn("加速档下 --steps 不生效（步数由官方 sigma 表决定）；要换档位请用 --turbo-steps N",
                 args.quiet)

    # ---- 附加 LoRA（第二槽位，与主槽位串联叠加）----
    lora2 = getattr(args, "lora2", None)
    if lora2:
        lora2 = resolve_lora(lora2)
        if not lora2:
            print(f"参数错误: 找不到 LoRA {args.lora2!r}（qwen.py loras 看列表）", file=sys.stderr)
            return EXIT_ARGS
        if any(x["file"] == lora2 and x.get("turbo") for x in S.list_loras()):
            print("参数错误: 附加槽位只接受普通 LoRA；加速档 LoRA 请放到 --lora", file=sys.stderr)
            return EXIT_ARGS
        if lora2 == lora:
            print("参数错误: --lora2 不能和 --lora 是同一个文件", file=sys.stderr)
            return EXIT_ARGS
        p["lora2"] = lora2
        p["lora2_weight"] = getattr(args, "lora2_weight", 1.0)

    # ---- VAE（解码器）----
    vae = getattr(args, "vae", None)
    if vae:
        vae = resolve_vae(vae)
        if vae is None:
            print(f"参数错误: 找不到 VAE {args.vae!r}（qwen.py vaes 看列表）", file=sys.stderr)
            return EXIT_ARGS
        if vae:            # 空串 = 显式要求官方/模板自带，不必写进 payload
            p["vae"] = vae

    if mode == "t2i":
        w, h = parse_size(args.size)
        p["width"], p["height"] = w, h
    else:
        try:
            p["ref_image"] = stage_ref(args.ref)
        except ValueError as e:
            print(f"参数错误: {e}", file=sys.stderr)
            return EXIT_ARGS
        p["resolution"] = args.res or 1024

    if mode != "t2i" and not args.prompt and mode == "edit":
        print("参数错误: edit 模式需要给编辑指令", file=sys.stderr)
        return EXIT_ARGS

    # ---- 构造工作流 ----
    try:
        wf, meta = S.build_workflow(mode, p)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return EXIT_ARGS

    if args.dry_run:
        print(json.dumps({"workflow": wf, "meta": meta},
                         ensure_ascii=False, indent=2))
        return EXIT_OK

    # ---- 提交 ----
    if not S.comfy_up():
        print(f"ComfyUI 未响应（{S.COMFY}）。先启动：双击 启动出图台.bat 或 启动ComfyUI.bat",
              file=sys.stderr)
        return EXIT_NOCOMFY

    g = gpu_busy()
    if g and g[2] > 60:
        log(f"⚠️ 独显利用率已 {g[2]}%（{g[0]}/{g[1]} MiB）—— 别的进程在跑，"
            f"本次出图会变慢，也可能拖慢它。", args.quiet)

    ok, res = S.submit(wf)
    if not ok:
        print(f"提交失败: {res}", file=sys.stderr)
        return EXIT_FAIL
    pid = res
    log(f"[提交] {S.RECIPES[mode]['label']}  id={pid}", args.quiet)
    log(f"       尺寸={meta.get('size')} 步数={meta['steps']} "
        f"cfg={meta['cfg'] if meta['cfg'] is not None else '关闭'} "
        f"seed={meta['seed']} {meta['sampler']}/{meta['scheduler']}"
        + (f" 张数={meta['count']}" if meta.get("count", 1) > 1 else ""), args.quiet)
    if meta.get("lora"):
        log(f"       LoRA={meta['lora']} @{meta['lora_weight']}"
            + (f"  ⚡加速档 {meta.get('turbo_steps')} 步"
               f"（sigmas=[{meta.get('turbo_nodes')}]）" if meta.get("turbo") else ""), args.quiet)
    if meta.get("lora2"):
        log(f"       附加LoRA={meta['lora2']} @{meta['lora2_weight']}（串联叠加）", args.quiet)
    if meta.get("vae"):
        log(f"       VAE={meta['vae']}", args.quiet)

    job = {
        "id": pid, "mode": mode, "mode_label": S.RECIPES[mode]["label"],
        "prompt": p.get("prompt", ""), "negative": p.get("negative", ""),
        "params": meta, "created": time.time(), "state": "queued",
        "elapsed": None, "images": [], "source": "cli",
    }
    S.job_add(job)

    # ---- 轮询 ----
    t0 = time.time()
    last = ""
    while time.time() - t0 < args.timeout:
        got = S.fetch_images(pid)
        if got is not None:
            if got["images"]:
                job.update(images=got["images"], elapsed=got["elapsed"], state="done")
                S.job_update(pid, images=got["images"], elapsed=got["elapsed"], state="done")
                srcs = [s for s in (find_output(im) for im in got["images"]) if s]
                outs = srcs
                log(f"[完成] {len(srcs)} 张  用时 {got['elapsed']}s", args.quiet)
                if args.out:
                    outs = copy_many(srcs, os.path.abspath(args.out))
                for s, o in zip(srcs, outs):
                    log(f"       {s}" + (f"  ->  {o}" if args.out else ""), args.quiet)
                if args.json:
                    print(json.dumps({
                        "ok": True, "output": outs[0], "source_output": srcs[0],
                        "outputs": outs, "source_outputs": srcs,
                        "count": len(outs),
                        "prompt": job["prompt"], "params": meta,
                        "elapsed": got["elapsed"],
                    }, ensure_ascii=False))
                else:
                    # 契约：stdout 末行是 `OUTPUT: <绝对路径>`；多张时逐行打印
                    for o in outs:
                        print(f"OUTPUT: {o}")
                return EXIT_OK
            state = "error" if got.get("error") else "interrupted"
            S.job_update(pid, state=state, error=got.get("error"))
            print(f"生成未成功（{state}）: {got.get('error') or '任务被中断，无产物'}",
                  file=sys.stderr)
            return EXIT_FAIL
        st, pos = S.queue_state(pid)
        cur = f"{st}" + (f" 前面还有 {pos} 个" if pos else "")
        if cur != last:
            log(f"[状态] {cur}", args.quiet)
            last = cur
        time.sleep(1.5)

    print(f"超时（{args.timeout}s）。任务可能仍在跑，可在出图台查看：http://127.0.0.1:8190",
          file=sys.stderr)
    return EXIT_TIMEOUT


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        prog="qwen.py",
        description="Qwen-Image-2.1 本地出图命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[1].split("通用选项")[0].strip()
        if "用法\n----" in __doc__ else None,
    )
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("--seed", type=int, default=None, help="随机种子，-1=随机")
        p.add_argument("--steps", type=int, default=None)
        p.add_argument("--cfg", type=float, default=None)
        p.add_argument("--negative", default=None)
        p.add_argument("--sampler", default=None)
        p.add_argument("--scheduler", default=None)
        p.add_argument("--batch", type=int, default=1, choices=[1, 2, 3, 4],
                       help="一次出几张（同一个提示词，一次采样出 N 张，1~4）")
        p.add_argument("--lora", default=None,
                       help="LoRA 文件名（在 models/loras 下）或 'turbo' 简写；"
                            "支持模糊匹配，如 'consistency'")
        p.add_argument("--lora-weight", type=float, default=1.0,
                       help="LoRA 权重（默认 1.0）")
        p.add_argument("--lora2", default=None,
                       help="附加 LoRA（第二槽位，与主槽位串联叠加）；只接受普通 LoRA，"
                            "加速档 LoRA 必须放 --lora。支持模糊匹配，如 'consistency'")
        p.add_argument("--lora2-weight", type=float, default=1.0,
                       help="附加 LoRA 权重（默认 1.0）")
        p.add_argument("--vae", default=None,
                       help="切换 VAE 解码器：'official'=模板自带 / 'texture'=texture-fix / "
                            "或文件名。用 `qwen.py vaes` 看全部")
        p.add_argument("--turbo", dest="turbo", action="store_true", default=None,
                       help="强制开加速档（官方 sigma 表 + euler，步数见 --turbo-steps）")
        p.add_argument("--no-turbo", dest="turbo", action="store_false",
                       help="强制关加速档（LoRA 照挂，走经典采样）")
        p.add_argument("--turbo-steps", type=int, default=None,
                       choices=[4, 5, 6, 7, 8],
                       help="加速档步数预设（官方 raw sigma 表）：6=默认推荐，"
                            "8=密集小字，4/5=旧版档")
        p.add_argument("--style", default=None,
                       help="风格预设（纯提示词，不需要 LoRA）：film / anime / ink / ... "
                            "用 `qwen.py styles` 看全部；不传 = 不加风格")
        p.add_argument("--boldness", default=None,
                       help="大胆度档位 L0/L1/L2/L3（只追加机位+姿势到提示词末尾，不动采样参数）")
        p.add_argument("-o", "--out", default=None, help="产物复制到该路径")
        p.add_argument("--timeout", type=int, default=900)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("t2i", help="文生图")
    p.add_argument("prompt", help="提示词")
    p.add_argument("--size", default=None, help="1024x1024 / 1280 / 768x1024")
    add_common(p)

    p = sub.add_parser("edit", help="参考图编辑（保身份）")
    p.add_argument("prompt", help="编辑指令")
    p.add_argument("-r", "--ref", required=True, help="参考图路径")
    p.add_argument("--res", type=int, default=None, help="参考图缩放长边")
    add_common(p)

    p = sub.add_parser("bgremove", help="背景移除抠图")
    p.add_argument("prompt", nargs="?", default="", help="可留空用默认抠图指令")
    p.add_argument("-r", "--ref", required=True, help="参考图路径")
    p.add_argument("--res", type=int, default=None)
    add_common(p)

    p = sub.add_parser("enhance", help="把想法扩写成图像描述（补位置/光线/材质/收尾）")
    p.add_argument("prompt")
    p.add_argument("--count", type=int, default=1, choices=[1, 2, 3],
                   help="生成几个不同方向的版本（1/2/3）")
    p.add_argument("--lang", default="en", choices=["en", "zh"],
                   help="描述语言：en 英文（推荐）/ zh 中文")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("status", help="看服务与显卡状态")
    p = sub.add_parser("list", help="列出可用模式与配方")
    p = sub.add_parser("loras", help="列出可用 LoRA（含加速档）")
    p = sub.add_parser("vaes", help="列出可切换的 VAE 解码器")
    p = sub.add_parser("styles", help="列出风格预设（纯提示词，不需要 LoRA）")
    p.add_argument("--nsfw", action="store_true", help="连成人向风格一起列出")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return EXIT_ARGS
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "loras":
        return cmd_loras(args)
    if args.cmd == "vaes":
        return cmd_vaes(args)
    if args.cmd == "styles":
        return cmd_styles(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "enhance":
        return cmd_enhance(args)
    return cmd_generate(args, args.cmd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
