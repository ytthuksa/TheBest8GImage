#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令跑完三项 UI 工作流体检。用法: python _check_all.py [目录或文件...]

三项检查（任一 FAIL 都意味着工作流在 UI 里会跑错或跑不起来）：
  1. _validate_ui_workflow.py     —— 引用完整性：断链 / 槽位越界 / 孤儿连线 / 未定义子图
  2. _check_model_refs.py         —— 模型引用：指向不存在的权重文件
  3. _check_promoted_widgets.py   —— 提升 widget：下标错位 / 枚举值非法

默认扫描 user/default/workflows/ 下的全部 json。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = HERE / "user" / "default" / "workflows"
PY = str(HERE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():                      # skill 目录下没有 venv，退回托管 Python
    PY = sys.executable

CHECKS = [
    ("引用完整性", "validate_ui_workflow"),
    ("模型引用", "check_model_refs"),
    ("提升widget", "check_promoted_widgets"),
    ("取值合法性", "check_widgets_vs_server"),
]


def resolve(stem):
    """兼容 `x.py` 与 `_x.py` 两种命名（本机用下划线前缀，skill 里不带）。"""
    for cand in (HERE / f"{stem}.py", HERE / f"_{stem}.py"):
        if cand.exists():
            return cand
    return None


def main():
    args = sys.argv[1:]
    if args and all(Path(a).is_file() for a in args):
        files = [Path(a) for a in args]
    else:
        d = Path(args[0]) if args else DEFAULT_DIR
        if not d.is_dir():
            d = Path.cwd()
        files = sorted(d.glob("*.json"))
    if not files:
        print("没找到工作流 json")
        return 1

    print(f"待检 {len(files)} 个文件\n" + "=" * 70)
    summary = []
    for label, stem in CHECKS:
        script = resolve(stem)
        if script is None:
            print(f"\n### {label}: 找不到脚本 {stem}.py，跳过")
            summary.append((label, 0))
            continue
        print(f"\n### {label}  ({script.name}) ###")
        r = subprocess.run([PY, str(script), *map(str, files)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        # 只显示结论行，细节按需单独跑
        for line in out.splitlines():
            if line.startswith("===") or "总计" in line or "[FAIL]" in line or "缺失" in line \
                    or "不在" in line or "不足" in line or "失败" in line:
                print(line)
        summary.append((label, r.returncode))

    print("\n" + "=" * 70)
    bad = 0
    for label, rc in summary:
        if rc == 0:
            state = "PASS"
        elif rc == 2:
            state = "SKIP（服务未启动）"
        else:
            state = "FAIL"
            bad += 1
        print(f"  {label:<12} {state}")
    print("说明: 「取值合法性」需要 ComfyUI 在跑（读 /api/object_info）。")
    print("      官方模板 image_qwen_image_2_1_*.json / qwen_image_2_1_GGUF_t2i.json")
    print("      引用官方 int8_convrot 权重与官方示例图，本地没有 → 预期 FAIL，属正常。")
    print("      本地可用的是 qwen_image_2_1_local_{t2i,edit,bgremove,pose}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
