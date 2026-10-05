# -*- coding: utf-8 -*-
"""UI 工作流（含 subgraph）引用完整性校验器。

浏览器冒烟测试只能发现「页面报错」，本校验器能逐条抓出断链/越界，覆盖 UI 文件最常见的损坏模式：
  1. links 数组里的每条连线，两端节点必须存在，槽位下标必须在范围内
  2. 每个节点 input 上挂的 link id 必须存在于 links，且该 link 的 target 必须指向本节点/本槽位
  3. 每个节点 output 的 links 列表里每个 id 必须存在，且该 link 的 origin 必须指向本节点/本槽位
  4. 反向：links 里出现的 link 必须被至少一端引用（否则是孤儿连线）
虚拟端点约定：-10 表示子图输入（提升输入），-20 表示子图输出。

用法：python _validate_ui_workflow.py <workflow.json> [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def normalize_links(links):
    """顶层 links 是旧版数组 [id, origin_id, origin_slot, target_id, target_slot, type]，
    子图 links 是对象 {id, origin_id, origin_slot, target_id, target_slot, type}。统一成对象。"""
    out = []
    for l in links:
        if isinstance(l, dict):
            out.append(l)
        elif isinstance(l, (list, tuple)) and len(l) >= 5:
            out.append({"id": l[0], "origin_id": l[1], "origin_slot": l[2],
                        "target_id": l[3], "target_slot": l[4],
                        "type": l[5] if len(l) > 5 else None})
        else:
            raise ValueError(f"无法识别的 link 结构: {l!r}")
    return out


def check_graph(nodes, links, label, allow_virtual=True):
    errs = []
    links = normalize_links(links)
    by_id = {n["id"]: n for n in nodes}
    link_by_id = {}

    for l in links:
        lid = l.get("id")
        if lid in link_by_id:
            errs.append(f"{label}: 重复的 link id {lid}")
        link_by_id[lid] = l
        o, t = l.get("origin_id"), l.get("target_id")
        os_, ts_ = l.get("origin_slot"), l.get("target_slot")

        if not allow_virtual or (isinstance(o, int) and o >= 0):
            if o not in by_id:
                errs.append(f"{label}: link {lid} 的 origin_id={o} 不存在")
            else:
                outs = by_id[o].get("outputs") or []
                if not isinstance(os_, int) or os_ >= len(outs):
                    errs.append(f"{label}: link {lid} origin_slot={os_} 越界 "
                                f"(node {o} 只有 {len(outs)} 个输出)")
        if not allow_virtual or (isinstance(t, int) and t >= 0):
            if t not in by_id:
                errs.append(f"{label}: link {lid} 的 target_id={t} 不存在")
            else:
                ins = by_id[t].get("inputs") or []
                if not isinstance(ts_, int) or ts_ >= len(ins):
                    errs.append(f"{label}: link {lid} target_slot={ts_} 越界 "
                                f"(node {t} 只有 {len(ins)} 个输入)")

    referenced = set()
    for n in nodes:
        for i, inp in enumerate(n.get("inputs") or []):
            lid = inp.get("link")
            if lid is None:
                continue
            referenced.add(lid)
            if lid not in link_by_id:
                errs.append(f"{label}: node {n['id']} input[{i}]('{inp.get('name')}') "
                            f"引用不存在的 link {lid}")
            else:
                l = link_by_id[lid]
                if l.get("target_id") != n["id"] or l.get("target_slot") != i:
                    errs.append(f"{label}: node {n['id']} input[{i}] 的 link {lid} "
                                f"实际指向 node {l.get('target_id')} slot {l.get('target_slot')}")
        for si, out in enumerate(n.get("outputs") or []):
            for lid in out.get("links") or []:
                referenced.add(lid)
                if lid not in link_by_id:
                    errs.append(f"{label}: node {n['id']} output[{si}]('{out.get('name')}') "
                                f"引用不存在的 link {lid}")
                else:
                    l = link_by_id[lid]
                    if l.get("origin_id") != n["id"] or l.get("origin_slot") != si:
                        errs.append(f"{label}: node {n['id']} output[{si}] 的 link {lid} "
                                    f"实际源是 node {l.get('origin_id')} slot {l.get('origin_slot')}")

    for lid in link_by_id:
        if lid not in referenced:
            errs.append(f"{label}: link {lid} 是孤儿（没有任何节点引用）")
    return errs, len(link_by_id)


def main(paths):
    total_err = 0
    for p in paths:
        path = Path(p)
        d = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n=== {path.name} ===")
        all_errs = []

        e, nl = check_graph(d.get("nodes") or [], d.get("links") or [], "top")
        all_errs += e
        print(f"  [top] nodes={len(d.get('nodes') or [])} links={nl} errors={len(e)}")

        for si, sg in enumerate((d.get("definitions") or {}).get("subgraphs") or []):
            e, nl = check_graph(sg.get("nodes") or [], sg.get("links") or [], f"sg{si}")
            all_errs += e
            print(f"  [sg{si}] {sg.get('name')} nodes={len(sg.get('nodes') or [])} "
                  f"links={nl} errors={len(e)}")

        # 子图实例的 type 必须能在 definitions 里找到
        sg_ids = {s.get("id") for s in (d.get("definitions") or {}).get("subgraphs") or []}
        for n in d.get("nodes") or []:
            if n.get("type") in sg_ids:
                print(f"  [ok] node {n['id']} 指向已定义的 subgraph {n['type'][:8]}…")
            elif isinstance(n.get("type"), str) and "-" in n["type"] and len(n["type"]) == 36:
                all_errs.append(f"top: node {n['id']} type={n['type']} 但 definitions 里没有这个 subgraph")

        if all_errs:
            print(f"  !! {len(all_errs)} 个问题：")
            for m in all_errs[:25]:
                print("     -", m)
        else:
            print("  ✅ 引用完整性检查通过")
        total_err += len(all_errs)
    print(f"\n总计问题数: {total_err}")
    return 1 if total_err else 0


if __name__ == "__main__":
    args = sys.argv[1:] or [r"C:\ComfyUI\user\default\workflows\qwen_image_2_1_local_edit.json",
                            r"C:\ComfyUI\user\default\workflows\qwen_image_2_1_local_t2i.json"]
    raise SystemExit(main(args))
