#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基于服务端权威 schema 校验工作流里的每一个 widget 取值。

为什么用服务端 schema：`GET /api/object_info` 是**后端自己注册的**节点定义，
COMBO 选项列表是后端真实可见的文件/枚举（**包含 .gguf**，因为 ComfyUI-GGUF 已加载），
比任何本地硬编码的枚举表都可靠。它同时给出 INT/FLOAT 的 min/max。

难点在于把 `widgets_values` 数组按位置对齐到节点输入。对齐规则（从 ComfyUI 前端行为反推）：
  1. 只有 widget 类型占位：INT / FLOAT / STRING / BOOLEAN / COMBO / COMFY_DYNAMICCOMBO_V3
     以及「类型本身是列表」的 COMBO；MODEL / IMAGE / COMFY_AUTOGROW_V3 等连线类型不占位。
  2. 带 `control_after_generate: true` 的 INT 后面会**多一个**控件 widget（如 KSampler.seed 后的 randomize）。
  3. `COMFY_DYNAMICCOMBO_V3` 的值是 option 的 key，随后紧跟该 option 的子输入
     （如 SaveImageAdvanced: filename_prefix, format='png', bit_depth, color_profile）。
  4. required 与 optional 的 widget 都占位（如 CLIPLoader 的 device 在 optional 里）。

用法: python _check_widgets_vs_server.py <workflow.json> [...] [--url http://127.0.0.1:8188]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

WIDGET_SCALAR = {"INT", "FLOAT", "STRING", "BOOLEAN"}
# 上传类 widget（前端专属，值是文件名或占位串，不校验内容）
UPLOAD_KINDS = {"IMAGEUPLOAD", "FILEUPLOAD", "AUDIOUPLOAD", "VIDEOUPLOAD", "UPLOAD"}


def fetch_object_info(url="http://127.0.0.1:8188"):
    # 必须绕过沙箱代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url.rstrip("/") + "/api/object_info", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def iter_widgets(node_def):
    """产出 (widget名, 类型描述, extra) —— 按前端 widgets_values 的真实顺序。"""
    for sect in ("required", "optional"):
        for name, spec in (node_def["input"].get(sect) or {}).items():
            t0 = spec[0] if spec else None
            extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(t0, list):                       # 静态 COMBO
                yield name, "COMBO", {"options": t0, **extra}
                if extra.get("image_upload"):
                    yield f"{name}#upload", "CONTROL", {}
            elif t0 == "COMBO":                            # 动态 COMBO（选项在 extra.options）
                yield name, "COMBO", extra
            elif t0 == "COMFY_DYNAMICCOMBO_V3":
                yield name, "DYNAMIC", extra
            elif t0 in WIDGET_SCALAR:
                yield name, t0, extra
                if t0 == "INT" and extra.get("control_after_generate"):
                    yield f"{name}#control", "CONTROL", {}
            elif isinstance(t0, str) and t0.upper() in UPLOAD_KINDS:
                yield name, "CONTROL", {}          # 上传按钮：占位但不校验内容
            # 其余（MODEL / IMAGE / COMFY_AUTOGROW_V3 / ...）是连线，不占位


def validate_value(name, kind, extra, val):
    """返回错误描述或 None。"""
    if kind in ("CONTROL", "STRING", "BOOLEAN"):
        return None
    if kind == "COMBO":
        opts = extra.get("options")
        if opts and val not in opts:
            return f"{name} = {val!r} 不在 {len(opts)} 个合法选项中（前5: {opts[:5]}）"
        return None
    if kind in ("INT", "FLOAT"):
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return f"{name} = {val!r} 不是数字"
        lo, hi = extra.get("min"), extra.get("max")
        if lo is not None and val < lo:
            return f"{name} = {val} < min {lo}"
        if hi is not None and val > hi:
            return f"{name} = {val} > max {hi}"
        return None
    return None


def walk_dynamic(opt, values, i, prefix):
    """COMFY_DYNAMICCOMBO_V3 的子输入：返回 (消耗个数, 错误列表)。"""
    errs, used = [], 0
    sub = ((opt or {}).get("inputs") or {}).get("required") or {}
    for nm, spec in sub.items():
        t0 = spec[0] if spec else None
        extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if isinstance(t0, list):
            kind, ex = "COMBO", {"options": t0, **extra}
        elif t0 == "COMBO":
            kind, ex = "COMBO", extra
        elif t0 in WIDGET_SCALAR:
            kind, ex = t0, extra
        else:
            continue
        if i + used >= len(values):
            errs.append(f"{prefix}.{nm}: widgets_values 不够长，取不到值")
            break
        v = values[i + used]
        e = validate_value(f"{prefix}.{nm}", kind, ex, v)
        if e:
            errs.append(e)
        used += 1
    return used, errs


def validate_node(ntype, values, defs, where):
    nd = defs.get(ntype)
    if nd is None:
        return [f"{where}: 服务端不认识节点类型 {ntype!r}"]
    errs, i, consumed = [], 0, []
    for name, kind, extra in iter_widgets(nd):
        if i >= len(values):
            errs.append(f"{where}: widgets_values 长度 {len(values)} 不足，缺 {name!r} 的值")
            break
        val = values[i]
        if kind == "DYNAMIC":
            opts = {o["key"]: o for o in (extra.get("options") or []) if isinstance(o, dict)}
            if opts and val not in opts:
                errs.append(f"{where}.{name} = {val!r} 不是合法格式 key（{list(opts)}）")
            used, sub_errs = walk_dynamic(opts.get(val), values, i + 1, f"{where}.{name}")
            errs += sub_errs
            consumed.append(f"{name}={val}")
            i += 1 + used
            continue
        e = validate_value(f"{where}.{name}", kind, extra, val)
        if e:
            errs.append(e)
        consumed.append(f"{name}={val}")
        i += 1
    if i != len(values):
        errs.append(f"{where}: widgets_values 有 {len(values)} 个，按 schema 只对齐到 {i} 个"
                    f"（多出 {values[i:]!r}）")
    return errs


def check(path, defs, verbose=False):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    errs, skipped = [], []
    n_nodes = 0
    sg_ids = {sg.get("id") for sg in (d.get("definitions", {}).get("subgraphs") or [])}

    def scan(nodes, tag):
        nonlocal n_nodes
        for n in nodes:
            t = n["type"]
            if t in sg_ids:
                continue                       # 子图实例：本身不是服务端节点
            if t not in defs:
                skipped.append(f"{tag}{t}({n['id']})")   # 前端专属节点（MarkdownNote 等）
                continue
            wv = n.get("widgets_values")
            if not isinstance(wv, list) or not wv:
                continue
            n_nodes += 1
            errs.extend(validate_node(t, wv, defs, f"{tag}{t}({n['id']})"))

    scan(d.get("nodes", []), "")
    for si, sg in enumerate(d.get("definitions", {}).get("subgraphs", []) or []):
        scan(sg.get("nodes", []), f"sg{si}/")
        # 子图实例：widget 型输入按序对齐（实例是子图，没有服务端 schema，只查长度）
        wt = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
        win = [it for it in (sg.get("inputs") or []) if it.get("type") in wt]
        for n in d.get("nodes", []):
            if n.get("type") != sg.get("id"):
                continue
            wv = n.get("widgets_values") or []
            if not isinstance(wv, list):
                continue
            if len(wv) != len(win):
                errs.append(f"实例{n['id']}: widgets_values={len(wv)} 但 widget 型输入={len(win)}")
            n_nodes += 1

    status = "FAIL" if errs else "PASS"
    print(f"\n=== {Path(path).name}  [{status}]  校验 {n_nodes} 个节点"
          f"（跳过 {len(skipped)} 个前端专属节点）===")
    if verbose and skipped:
        print("  跳过: " + ", ".join(skipped))
    for e in errs:
        print("  " + e)
    return len(errs)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    url = "http://127.0.0.1:8188"
    for a in sys.argv[1:]:
        if a.startswith("--url"):
            url = a.split("=", 1)[1] if "=" in a else url
    if not args:
        print(__doc__)
        return 1
    try:
        defs = fetch_object_info(url)
    except Exception as e:
        print(f"拉取 object_info 失败（ComfyUI 没起？）：{e}")
        return 2
    print(f"服务端节点定义 {len(defs)} 个（{url}）")
    total = sum(check(p, defs, verbose) for p in args)
    print(f"\n总计问题数: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
