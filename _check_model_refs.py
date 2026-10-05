#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UI 工作流「模型引用」校验器 —— 抓出指向不存在文件的悬空模型名。

为什么需要：subgraph 的「提升 widget」语义下，**子图实例上的 widget 值会覆盖内部节点的值**。
所以只改内部节点、忘了改实例，工作流在 UI 里加载就会指向不存在的模型（例如官方模板留下的
qwen_image_2.1_int8_convrot.safetensors），而纯结构校验器查不出来。

注意：不要用 folder_paths.get_filename_list() 做这个检查 —— 它默认**过滤掉 .gguf**，
因为 GGUF 扩展名是 ComfyUI-GGUF 自定义节点在 main.py 启动时才注册的，裸 import 拿不到，
会把真实存在的 .gguf 误报为缺失。这里直接扫模型目录。

用法：python _check_model_refs.py <workflow.json> [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MODELS_DIR = None


def _find_models_dir():
    """按优先级定位 models/ 目录，使脚本在 ComfyUI 根目录或 skill 目录下都能跑。"""
    import os
    env = os.environ.get("COMFYUI_ROOT")
    cands = [Path(__file__).resolve().parent / "models",
             Path(env) / "models" if env else None,
             Path("C:/ComfyUI/models"),
             Path.cwd() / "models"]
    for c in cands:
        if c and c.is_dir():
            return c
    return Path(__file__).resolve().parent / "models"


MODEL_EXTS = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft"}

# 节点类型 -> 是否需要在 models/ 下找到对应文件
MODEL_NODE_TYPES = {
    "UnetLoaderGGUF", "UnetLoaderGGUFAdvanced", "UnetLoader",
    "CLIPLoader", "CLIPLoaderGGUF", "DualCLIPLoader", "TripleCLIPLoader",
    "VAELoader", "CheckpointLoaderSimple", "LoraLoader", "LoraLoaderModelOnly",
}
# 每个节点类型里模型名所在的 widget 下标
NAME_SLOT = {
    "UnetLoaderGGUF": 0, "UnetLoaderGGUFAdvanced": 0, "UnetLoader": 0,
    "CLIPLoader": 0, "CLIPLoaderGGUF": 0, "DualCLIPLoader": 0,
    "TripleCLIPLoader": 0, "VAELoader": 0, "CheckpointLoaderSimple": 0,
    "LoraLoader": 0, "LoraLoaderModelOnly": 0,
}
# 子图「提升 widget」的语义名 —— 这些输入在实例上也会存一份值, 且会覆盖内部节点
MODEL_WIDGET_NAMES = {
    "unet_name", "clip_name", "vae_name", "ckpt_name", "lora_name",
    "model_name", "text_encoder_name", "clip_name1", "clip_name2", "clip_name3",
}
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def index_models():
    """扫 models/ 下所有模型文件, 返回 {basename: 相对路径} 与 {相对路径(小写斜杠): 1}。"""
    global MODELS_DIR
    MODELS_DIR = _find_models_dir()
    by_base, by_rel = {}, {}
    if not MODELS_DIR.is_dir():
        return by_base, by_rel
    for p in MODELS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in MODEL_EXTS:
            rel = p.relative_to(MODELS_DIR).as_posix()
            by_base.setdefault(p.name, rel)
            by_rel[rel.lower()] = rel
    return by_base, by_rel


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    by_base, by_rel = index_models()
    print(f"模型索引: {len(by_base)} 个文件 (models/)")

    bad = 0
    for p in sys.argv[1:]:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        refs = []

        def scan(nodes, tag):
            for n in nodes:
                t = n.get("type")
                if t in MODEL_NODE_TYPES:
                    wv = n.get("widgets_values") or []
                    idx = NAME_SLOT.get(t, 0)
                    if isinstance(wv, list) and len(wv) > idx:
                        refs.append((t, wv[idx], f"{tag}node {n.get('id')}"))

        scan(d.get("nodes", []), "")
        for si, sg in enumerate(d.get("definitions", {}).get("subgraphs", []) or []):
            scan(sg.get("nodes", []), f"subgraph[{si}] ")
            # 子图实例: 按 sg.inputs 顺序配对 widgets_values（跳过非 widget 类型）
            win = [it for it in (sg.get("inputs") or []) if it.get("type") in WIDGET_TYPES]
            for n in d.get("nodes", []):
                if n.get("type") != sg.get("id"):
                    continue
                wv = n.get("widgets_values") or []
                if not isinstance(wv, list):
                    continue
                for it, val in zip(win, wv):
                    nm = it.get("name", "")
                    if nm in MODEL_WIDGET_NAMES:
                        refs.append((f"实例{n.get('id')}:{nm}", val,
                                     f"subgraph[{si}] 实例 {n.get('id')}"))

        errs, n_bad = [], 0
        for t, name, where in refs:
            if name is None:
                continue
            hit = by_base.get(Path(str(name)).name) if isinstance(name, str) else None
            if hit:
                errs.append(f"  [ok  ] {where}  {t}  ->  {name}  ({hit})")
            else:
                n_bad += 1
                errs.append(f"  [缺失] {where}  {t}  ->  {name!r}")
        status = "FAIL" if n_bad else "PASS"
        if n_bad:
            bad += 1
        print(f"\n=== {Path(p).name}  [{status}]  {n_bad} 处悬空 / 共 {len(refs)} 处引用 ===")
        for e in errs:
            print(e)

    print(f"\n总计: {len(sys.argv)-1} 个文件, {bad} 个有悬空模型引用")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
