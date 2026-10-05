#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""子图「提升 widget」映射校验器 + 下标映射表生成器。

背景：ComfyUI 新 subgraph 里，内部节点的 widget 被提升为子图输入后，
  内部节点该 widget 变成带 link 的 input（`inputs[i]['widget']={'name':...}`），
  值由**子图实例**的 widgets_values 经 link 注入 —— **实例值覆盖内部节点值**。
  ⇒ 「只改内部节点、忘了改实例」会让 UI 里跑的是错参数（我们踩过：
    官方模板残留的 qwen_image_2.1_int8_convrot.safetensors 悬空引用）。

两个容易搞错的点（都踩过）：
  1. **不能拿子图输入名去查内部 widget 下标** —— 上游命名不一致，
     子图输入叫 `scheduler`（label 是 sampler），实际驱动的却是 KSampler 的 `sampler_name`。
     唯一可靠办法是顺着 link 的 **target_slot** 反查内部节点 `inputs[target_slot]['name']`。
  2. **「实例值 != 内部节点值」是正常现象**，内部那份只是兜底值，不用修。
     真正要查的是「实例值本身是否合法」（模型文件存在、枚举值有效）。

用法：python _check_promoted_widgets.py <workflow.json> [...]  [-v]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}

try:
    import comfy.samplers as _cs
    SAMPLERS = set(_cs.KSampler.SAMPLERS)
    SCHEDULERS = set(_cs.KSampler.SCHEDULERS)
except Exception:                                     # 脱离 ComfyUI 环境时的降级
    SAMPLERS = SCHEDULERS = None

ENUM_WIDGETS = {"sampler_name": SAMPLERS, "scheduler": SCHEDULERS}


def normalize_links(links):
    out = []
    for l in links or []:
        if isinstance(l, dict):
            out.append(l)
        elif isinstance(l, (list, tuple)) and len(l) >= 5:
            out.append({"id": l[0], "origin_id": l[1], "origin_slot": l[2],
                        "target_id": l[3], "target_slot": l[4]})
    return out


def fmt(v):
    s = repr(v)
    return s if len(s) <= 46 else s[:43] + "..."


def check(path, verbose=False):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    problems, table = [], []

    for si, sg in enumerate(d.get("definitions", {}).get("subgraphs", []) or []):
        win = [it for it in (sg.get("inputs") or []) if it.get("type") in WIDGET_TYPES]
        link_by_id = {l["id"]: l for l in normalize_links(sg.get("links"))}
        inner = {n["id"]: n for n in sg.get("nodes", [])}

        for inst in d.get("nodes", []):
            if inst.get("type") != sg.get("id"):
                continue
            wv = inst.get("widgets_values") or []
            if len(wv) != len(win):
                problems.append(
                    f"  [长度] 实例 {inst.get('id')}: widgets_values={len(wv)} "
                    f"但 widget 型输入={len(win)} —— 配对会错位")
            for k, (it, val) in enumerate(zip(win, wv)):
                nm = it.get("name")
                lids = it.get("linkIds") or []
                if not lids:
                    table.append((k, nm, "(未接线)", val, ""))
                    continue
                for lid in lids:
                    lk = link_by_id.get(lid)
                    if lk is None:
                        problems.append(f"  [断链] 提升输入 {nm!r} 的 link {lid} 不在子图 links 里")
                        continue
                    tn = inner.get(lk["target_id"])
                    if tn is None:
                        problems.append(f"  [断链] link {lid} 目标节点 {lk['target_id']} 不在子图内")
                        continue
                    # ★ 用 target_slot 反查真实 widget 名
                    t_in = (tn.get("inputs") or [])
                    slot = lk["target_slot"]
                    if slot >= len(t_in):
                        problems.append(f"  [越界] link {lid} target_slot={slot} 超出 {tn['type']} 的 inputs")
                        continue
                    true_name = t_in[slot].get("name")
                    alias = "" if true_name == nm else f" (输入名 {nm!r} ≠ widget {true_name!r})"
                    table.append((k, nm, f"{tn['type']}({tn['id']}).{true_name}", val, alias))
                    # 枚举合法性
                    allowed = ENUM_WIDGETS.get(true_name)
                    if allowed and isinstance(val, str) and val not in allowed:
                        problems.append(
                            f"  [非法值] {tn['type']}({tn['id']}).{true_name} = {val!r} "
                            f"不在合法集合内（共 {len(allowed)} 个）")

    status = "FAIL" if problems else "PASS"
    print(f"\n=== {Path(path).name}  [{status}] ===")
    print("  #  子图输入名        -> 实际驱动                    值")
    for k, nm, tgt, val, alias in table:
        print(f"  {k:>2}  {nm:<17} -> {tgt:<28} {fmt(val)}{alias}")
    for p in problems:
        print(p)
    return len(problems)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    if not args:
        print(__doc__)
        return 1
    if SAMPLERS is None:
        print("注意: 未能加载 comfy.samplers，枚举合法性检查已跳过")
    total = sum(check(p, verbose) for p in args)
    print(f"\n总计问题数: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
