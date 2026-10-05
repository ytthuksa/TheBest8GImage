#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen-Image-2.1 本地 Web 前端 —— 后端桥接器（纯标准库，零第三方依赖）

设计要点
--------
* 只依赖 Python 标准库（本机 pip 装不了新包）。用 http.server + urllib。
* 本身不跑模型，所有推理转发给本机 ComfyUI（默认 127.0.0.1:8188）。
* 三档任务：t2i（文生图）/ edit（参考图编辑）/ bgremove（背景移除）。
  配方沿用实测最优：res_multistep + simple，cfg1.0→15 步，cfg2.5→20 步。
* 提示词润色转交同目录的 `_prompt_eng.py`（按 Qwen 官方 PE-T2I 八步法实现）：
  首选官方 PE 模型 `qwen-image-pe-t2i`（Ollama 加载的 Q4_K_M GGUF），
  未装则回落 `huihui_ai/qwen2.5-abliterate:7b-instruct`，
  Ollama 整个不可用再回落离线结构增强。全程 num_gpu=0 + keep_alive=0，零显存占用。
  支持一次出 1~3 个方向的版本（写实摄影 / 电影感 / 插画平面）与中英双语描述。

启动
----
    cd C:\\ComfyUI && .\\.venv\\Scripts\\python.exe _server.py
    浏览器打开 http://127.0.0.1:8190

环境变量
--------
    WEB_PORT    前端端口，默认 8190
    COMFY_URL   ComfyUI 地址，默认 http://127.0.0.1:8188
    OLLAMA_URL  Ollama 地址，默认 http://127.0.0.1:11434
    OLLAMA_MODEL 润色兜底模型，默认 huihui_ai/qwen2.5-abliterate:7b-instruct
    PE_MODEL   官方提示词扩写模型，默认 qwen-image-pe-t2i
    PE_NUM_GPU PE 模型卸载到显卡的层数，默认 0（纯 CPU，不抢显存）
"""

import copy
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 提示词工程模块（同目录，参考 Qwen 官方 PE-T2I system prompt 实现）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _prompt_eng as PE  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "_web")
# Fisher Pose 插件的编辑器前端（给「姿势编辑」Tab 的 iframe 用）。
# ⚠️ 必须由**本服务**代理，不能让 iframe 直接指向 8188 ——
#    freepose.mjs 的握手是 `event.origin !== location.origin` 就丢弃消息（见其第 751 行），
#    跨端口（8190 vs 8188）握不上手，编辑器会一直卡在「正在加载」。
FISHER_WEB = os.path.join(BASE, "custom_nodes", "ComfyUI-Fisher-Pose", "web")
JOBS_FILE = os.path.join(BASE, "_web_jobs.json")

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
PORT = int(os.environ.get("WEB_PORT", "8190"))
# 监听地址：默认只监听本机。设为 0.0.0.0 可让同一局域网内的手机/平板访问。
HOST = (os.environ.get("WEB_HOST", "127.0.0.1") or "127.0.0.1").strip()
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get(
    "OLLAMA_MODEL", "huihui_ai/qwen2.5-abliterate:7b-instruct"
)

# 实测最优配方（res_multistep 比官方 euler 快 44~48%，质量等价）
RECIPES = {
    "t2i": {
        "label": "文生图",
        "steps": 15,
        "cfg": 1.0,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "resolution": 1024,
        "prefix": "Qwen21_web_t2i",
        # viggle-turbo 加速档实测墙钟加速比（63.1s -> 30.1s）
        "turbo_speedup": 2.1,
        "template": "_api_t2i.json",
    },
    "edit": {
        "label": "参考图编辑",
        "steps": 20,
        "cfg": 2.5,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "resolution": 1024,
        "prefix": "Qwen21_web_edit",
        # 188.6s -> 58.2s
        "turbo_speedup": 3.2,
        "template": "_api_edit2.json",
    },
    "bgremove": {
        "label": "背景移除",
        "steps": 15,
        "cfg": 1.0,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "resolution": 1024,
        "prefix": "Qwen21_web_bgremove",
        # ~87s -> 56.1s
        "turbo_speedup": 1.6,
        "template": "_api_bgremove_cfg1.json",
    },
    # 姿势编辑（Fisher Pose + VNCCS PoseStudio LoRA，2026-10-04 接入）
    # 参数照抄 Fisher / VNCCS 官方推荐：25 步、cfg 1、euler / simple。
    # 这一档**不参与加速档** —— viggle-turbo 是给「经典文生图/编辑」的学生模型，
    # 和 VNCCS 姿势 LoRA 叠加会互相打架，所以 turbo 一律禁用。
    # 模板里 LoRA 是**固定挂载**的（strength 1.0），不走通用 LoRA 选择逻辑。
    "pose": {
        "label": "姿势编辑",
        "steps": 25,
        "cfg": 1.0,
        "sampler": "euler",
        "scheduler": "simple",
        "resolution": 1024,
        "prefix": "Qwen21_web_pose",
        "fixed_lora": "VNCCS_QI2_PoseStudioV1.1.safetensors",
        "template": "_api_pose.json",
    },
}

BGREMOVE_PROMPT = "Remove the background, and output a PNG image"

# --------------------------------------------------------------------------
# LoRA
# --------------------------------------------------------------------------
LORA_DIR = os.path.join(BASE, "models", "loras")

# viggle-turbo 加速 LoRA：装的是「运行时旁路」节点（y = Wx + BAx），不合并进权重。
# 它必须配 ViggleTurboSigmas 的自定义 sigma 表 —— 用 KSampler 自带 scheduler
# 会「过拟合锐利」（画面脏）。
#
# 下面这张表是官方 README「Rules that matter」+ 节点 tooltip 给的**官方预设**。
# 表里是 **raw（未 shift）节点** —— ViggleTurboSigmas 内部会按分辨率算 mu 再 shift，
# 所以任何分辨率都原样传，不用改。
#
# ⚠️ 硬规则：低噪声端 0.875 / 0.75 / 0.5 / 0.25 是学生模型训练时的落点，**不能动**。
#    加减步数只能在 1 → 0.875 这段高噪声区均匀切分 —— 因为构图是在 raw sigma
#    1 ~ 0.875 之间（shift 后大约前 5% 噪声）决定的，那一段跨太大就会「鬼影 + 布局漂移」。
#    移动低噪声节点画面会明显变软（Laplacian 锐度 0.014 vs 0.020）。
# ⚠️ 6 步表 = 4 步训练表 linspace(1, 1/4, 4) 把最噪声那一段 1 → 0.75 切成三份。
TURBO_SCHEDULES = {
    4: ("1.0, 0.75, 0.5, 0.25",
        "4 步 · 训练 schedule",
        "学生模型的训练档。细节和多样性比 6 步略低，v0.1 LoRA 只能用这档"),
    5: ("1.0, 0.875, 0.75, 0.5, 0.25",
        "5 步 · 启动 schedule",
        "v0.2 发布时用的档。约 4% 的提示词布局会漂，官方不建议再选"),
    6: ("1.0, 0.9375, 0.875, 0.75, 0.5, 0.25",
        "6 步 · 官方推荐",
        "v0.2.1 标准档，0% 布局漂移 —— 默认就用这个"),
    7: ("1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25",
        "7 步",
        "0% 漂移、比 6 步略锐，但官方原话是「不比 6 步明显更好」"),
    8: ("1.0, 0.9375, 0.875, 0.75, 0.625, 0.5, 0.25, 0.125",
        "8 步 · 密集文字",
        "画面里要渲染小字（招牌 / 海报 / 字幕）时选它，官方说小字明显更干净"),
}

# LoRA → 它支持的步数档。`steps` 是默认档，`schedules` 是可选项。
# `rank` 只影响下拉里的排序：官方说 v0.1「没有理由优先它」，所以排后面。
TURBO_LORAS = {
    "Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors": {
        "label": "viggle-turbo v0.2.1 r128",
        "steps": 6,
        "schedules": [4, 5, 6, 7, 8],
        "rank": 0,
        "note": "官方 ComfyUI workflow 用的档，实测比 15 步基线快 2.1x",
    },
    "Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors": {
        "label": "viggle-turbo v0.1 r64",
        "steps": 4,
        "schedules": [4],
        "rank": 1,
        "note": "旧版。官方用法是「4 步 + 不传 sigmas」，且原话「没有理由优先它」，仅作对照",
    },
}


def turbo_nodes(steps):
    """步数 → 官方 raw sigma 节点串。"""
    row = TURBO_SCHEDULES.get(int(steps))
    if row is None:
        raise ValueError(
            f"不支持的加速档步数：{steps}（官方预设只有 "
            f"{'/'.join(str(k) for k in sorted(TURBO_SCHEDULES))} 步）")
    return row[0]


# --------------------------------------------------------------------------
# 风格预设（纯提示词，不需要 LoRA）
# --------------------------------------------------------------------------
# 来源：Z-Image 那套「风格库」（47 个 LoRA + assets/styles/styles.json）的意图。
# 那 47 个 LoRA 是 Z-Image-Turbo 专用的，对 Qwen 完全无用（3840 vs 4096 维、
# 30 vs 32 层、合并 vs 分离 QKV），而且 styles.json 随包删除且没备份。
#
# 2026-09-30 实测：**Qwen-Image-2.1 纯提示词就能控制风格**，4/4 生效，含最难的二次元
# （Z-Image 当年必须挂动漫 LoRA 才出得来）。命中率 4 风格 × 3 种子 = 100%
# （同种子 baseline 对照，指标 + 肉眼双确认）。所以这里全部是提示词块，零下载。
# 详见 C:\ComfyUI\_styletest\验证报告.md
#
# ⚠️ 写风格块的铁律（都是从实测里总结出来的，别丢）：
#   1. **2D / 绘画类必须写「三连否定」** `not photorealistic, not 3D render, not photograph`。
#      不写的话模型会在「写实底子」和「绘画词」之间骑墙，出来是半写实的怪东西。
#   2. **英文词锚定风格，中文词补细节**。中英混写比纯中文稳。
#   3. **风格块加在提示词最前面**（复刻 Z-Image `_compose_prompt()` 的注入位置）。
#      放后面会被主体描述稀释。
#   4. **负向词别写成风格的对立面**。宝丽来要「低对比」，你若在负向里写「低对比」，
#      两边会打架。负向只放质量类（模糊 / 畸变 / 水印）。
STYLE_PRESETS = {
    # ── 摄影向 ────────────────────────────────────────────────────────
    "cinematic": {
        "label": "电影感",
        "block": "cinematic film still, anamorphic lens, dramatic chiaroscuro lighting, "
                 "teal and orange color grading, shallow depth of field, volumetric haze, "
                 "电影剧照，变形宽银幕镜头，戏剧性明暗对比，青橙色调，浅景深，体积雾，",
        "tip": "剧照质感：宽银幕 + 青橙调 + 强明暗",
    },
    "film": {
        "label": "胶片",
        "block": "35mm analog film photography, Kodak Portra 400, visible organic film grain, "
                 "soft halation, gentle highlight rolloff, slight vignette, "
                 "muted natural color rendition, "
                 "胶片摄影，柯达胶片色彩，可见胶片颗粒，柔和光晕，轻微暗角，",
        "tip": "柯达负片：颗粒 + 光晕 + 低饱和（实测饱和/锐度/色数都掉到 baseline 的一半左右）",
    },
    "polaroid": {
        "label": "宝丽来",
        "block": "Polaroid SX-70 instant film photo, faded warm washed-out tones, low contrast, "
                 "soft dreamy focus, visible white instant-film frame border, "
                 "vintage amateur family snapshot, 1980s color shift, "
                 "宝丽来拍立得，褪色暖调，低对比，柔和虚焦，白色相纸边框，复古业余快照，",
        "tip": "会真的渲染出白色相纸边框，但边框会吃掉一部分画面",
    },
    "magazine": {
        "label": "时尚杂志",
        "block": "high-fashion editorial photography, Vogue magazine cover, "
                 "dramatic directional studio lighting, polished professional retouching, "
                 "medium-format digital clarity, flawless skin, "
                 "时尚杂志大片，戏剧性布光，精致修图，中画幅数码质感，",
        "tip": "编辑大片：颗粒最低（精修无噪），构图会被改成近景肖像",
    },
    "snapshot": {
        "label": "写实快照",
        "block": "candid amateur smartphone photo, natural available light, "
                 "unposed everyday moment, slight motion blur, authentic unretouched look, "
                 "realistic skin texture and pores, "
                 "随手拍生活照，自然现场光，抓拍日常瞬间，轻微动态模糊，未经修饰的真实感，真实皮肤质感，",
        "tip": "随手拍：现场光 + 抓拍感 + 真实皮肤（用来压「AI 味」）",
    },
    "portrait": {
        "label": "商业人像",
        "block": "professional studio portrait, Rembrandt lighting, seamless backdrop, "
                 "85mm f/1.4, sharp catchlights in eyes, commercial beauty retouching, "
                 "商业影棚人像，伦勃朗光，无缝背景纸，85mm f/1.4，眼神光清晰，商业美妆修图，",
        "tip": "影棚布光 + 眼神光，适合头像类",
    },
    "ootd": {
        "label": "时尚街拍",
        "block": "street style fashion photography, full-body lookbook shot, "
                 "complete outfit visible head to toe, urban backdrop, editorial styling, "
                 "crisp fabric detail and texture, "
                 "时尚街拍，全身穿搭 lookbook，完整交代服装，都市背景，编辑级造型，清晰面料细节，",
        "tip": "全身完整交代服装（对应 Z-Image 里用得最多的 OOT 穿搭）",
    },

    # ── 绘画 / 2D 向（都要三连否定）────────────────────────────────────
    "anime": {
        "label": "二次元动漫",
        "block": "Japanese anime illustration, 2D cel-shaded animation still, "
                 "bold clean linework, flat vivid colors, anime key visual, painted background, "
                 "not photorealistic, not 3D render, not photograph, "
                 "动漫插画，赛璐璐上色，清晰线稿，平涂鲜明色彩，",
        "tip": "赛璐璐平涂 + 线稿。三连否定不能删（删了会骑墙成半写实）",
    },
    "anime_art": {
        "label": "动漫厚涂",
        "block": "anime digital painting, semi-realistic anime art, "
                 "soft cel shading blended with painterly rendering, highly detailed illustration, "
                 "luminous rim light, delicate facial features, "
                 "not photorealistic, not 3D render, "
                 "动漫厚涂插画，半写实动漫美术，柔和赛璐璐与绘画质感，精细插画，",
        "tip": "介于二次元与写实之间的厚涂插画",
    },
    "watercolor": {
        "label": "水彩",
        "block": "watercolor painting, wet-on-wet washes, visible paper grain and pigment blooming, "
                 "soft bleeding edges, translucent layered color, loose expressive brushwork, "
                 "not photorealistic, not 3D, not digital art, "
                 "水彩画，湿画法晕染，可见纸纹与颜料扩散，柔和渗色边缘，透明叠色，写意笔触，",
        "tip": "湿画法晕染 + 纸纹，画面偏轻透",
    },
    "ink": {
        "label": "水墨",
        "block": "traditional Chinese ink wash painting, sumi-e, monochrome ink on rice paper, "
                 "expressive confident brush strokes, generous negative space, "
                 "subtle ink diffusion and dry-brush texture, "
                 "中国水墨画，写意，宣纸墨色，酣畅笔触，大量留白，墨韵渗化与飞白，",
        "tip": "水墨写意 + 留白（别再加「彩色」类词）",
    },
    "lineart": {
        "label": "黑白线稿",
        "block": "black and white manga line art, clean confident ink linework, "
                 "screentone shading, high contrast monochrome, no color, "
                 "黑白漫画线稿，干净利落的墨线，网点阴影，高对比单色，无彩色，",
        "negative": "彩色，上色，水彩，油画",
        "tip": "纯黑白线稿（负向里压了「彩色」，否则模型爱偷偷上色）",
    },
    "cg": {
        "label": "3D CG 渲染",
        "block": "3D CG render, octane render, subsurface scattering skin, "
                 "ray-traced global illumination, physically based materials, "
                 "high-poly detail, studio HDRI lighting, "
                 "3D 渲染，次表面散射皮肤，光线追踪全局光照，PBR 材质，高精度模型，影棚 HDRI 布光，",
        "tip": "CG 渲染质感（干净、非真人摄影的那种）",
    },
    "retro_scifi": {
        "label": "90s 复古科幻动画",
        "block": "1990s retro sci-fi anime, cel animation with film grain and chromatic aberration, "
                 "CRT scanline glow, vaporwave neon palette, hand-painted cels, "
                 "not photorealistic, not 3D render, "
                 "90年代复古科幻动画，赛璐璐动画质感，胶片颗粒与色差，CRT 扫描线辉光，蒸汽波霓虹配色，",
        "tip": "老动画录像带质感（对应 Z-Image 的 retro_scifi-90s）",
    },
    "dreamy": {
        "label": "梦幻氛围",
        "block": "ethereal dreamlike atmosphere, soft volumetric light rays, "
                 "pastel gradient palette, floating particles and bokeh, surreal poetic mood, "
                 "梦幻空灵氛围，柔和体积光，马卡龙渐变色调，漂浮微粒与光斑，超现实诗意，",
        "tip": "空灵梦幻（对应 MoonlightCAT / teenymooddy / melancholy 那批）",
    },

    # ── 成人向（UI 默认隐藏，跟 Z-Image 的 NSFW 总开关一致）──────────────
    # 提示词沿用 Z-Image 项目里验证过的「写实纪实私房」路线 —— 走 candid / documentary，
    # **不走 editorial / high-end fashion**：那条路与时尚型录词叠加会被推成「时尚插画/海报」味。
    "real_adult": {
        "label": "写实成人摄影",
        "nsfw": True,
        "block": "candid intimate photography, real human body, natural relaxed pose, "
                 "soft organic body curvature, gentle everyday bedroom or hotel-room setting, "
                 "warm available window light, visible skin pores and natural skin texture, "
                 "shot on 85mm lens, shallow depth of field, authentic unretouched amateur look, "
                 "raw photo, photorealistic, true-to-life, "
                 "not illustration, not anime, not painting, not poster, "
                 "真实感私房摄影，真实人体，自然放松的姿态，柔和的身体曲线，"
                 "日常卧室或酒店房间的真实场景，温暖的现场自然窗光，可见的皮肤毛孔与自然肌肤纹理，"
                 "85mm 镜头拍摄，浅景深，未经修饰的真实业余质感，RAW 照片，写实摄影，"
                 "不是插画，不是动漫，不是绘画，不是海报",
        "tip": "把「木偶感 / 漫画感 / 时尚海报感」拽回真实人体摄影",
    },
    "nude_art": {
        "label": "艺术人体",
        "nsfw": True,
        "block": "fine art nude photography, classical figure study, "
                 "sculptural body form, chiaroscuro studio lighting, "
                 "tasteful elegant composition, museum-quality art print, "
                 "艺术人体摄影，古典人体造型研究，雕塑感的形体，明暗对照布光，"
                 "优雅高级的构图，美术馆级艺术印相，",
        "tip": "艺术人体（明暗对照 + 雕塑感，偏艺术印相而非情色）",
    },
}

# 展示顺序：摄影向 → 绘画向 → 梦幻 → 成人向
STYLE_ORDER = [
    "cinematic", "film", "polaroid", "magazine", "snapshot", "portrait", "ootd",
    "anime", "anime_art", "watercolor", "ink", "lineart", "cg", "retro_scifi", "dreamy",
    "real_adult", "nude_art",
]


def style_list():
    """给前端 / CLI 的风格清单（按展示顺序，含成人向标记）。"""
    out = []
    for k in STYLE_ORDER:
        s = STYLE_PRESETS.get(k)
        if not s:
            continue
        out.append({
            "key": k,
            "label": s["label"],
            "tip": s.get("tip") or "",
            "nsfw": bool(s.get("nsfw")),
            "block": s.get("block") or "",
        })
    return out


def apply_style(p, prompt, negative):
    """
    把风格块注入提示词、把风格负向词并进负向词。返回 (prompt, negative, style_key, style_label)。

    ⚠️ 块加在**最前面** —— 复刻 Z-Image `_compose_prompt()` 的位置。放后面会被主体稀释。
    """
    key = (p.get("style") or "").strip()
    if not key or key == "none":
        return prompt, negative, "", ""
    st = STYLE_PRESETS.get(key)
    if st is None:
        raise ValueError(
            f"未知风格：{key}（可选 {'/'.join(STYLE_ORDER)}，或 none）")
    blk = (st.get("block") or "").strip()
    if blk:
        prompt = f"{blk} {prompt}".strip()
    add = (st.get("negative") or "").strip()
    if add:
        negative = ", ".join(x for x in (negative, add) if x)
    return prompt, negative, key, st["label"]


def list_loras():
    """扫描 models/loras，标注哪些是 viggle-turbo 加速 LoRA。"""
    out = []
    if not os.path.isdir(LORA_DIR):
        return out
    for fn in sorted(os.listdir(LORA_DIR)):
        if not fn.lower().endswith((".safetensors", ".ckpt", ".pt")):
            continue
        fp = os.path.join(LORA_DIR, fn)
        try:
            mb = round(os.path.getsize(fp) / 1024 / 1024, 1)
        except OSError:
            mb = None
        meta = TURBO_LORAS.get(fn)
        steps = (meta or {}).get("steps")
        out.append({
            "file": fn,
            "label": (meta or {}).get("label") or fn,
            "turbo": meta is not None,
            "nodes": turbo_nodes(steps) if steps else None,
            "steps": steps,
            "schedules": (meta or {}).get("schedules") or [],
            "rank": (meta or {}).get("rank", 99),
            "note": (meta or {}).get("note"),
            "size_mb": mb,
        })
    # 加速档排前面；同组内按 rank（官方推荐档优先），再按文件名
    out.sort(key=lambda x: (not x["turbo"], x["rank"], x["file"]))
    return out


# 「一次出几张」用的插入节点号（模板里只用了 1~8，不冲突）
_BATCH_NODE = "90"
MAX_BATCH = 4

# turbo 变换插入的节点号（模板里只用了 1~8 / 90）
_T_LORA = "91"
_T_GUIDER = "92"
_T_NOISE = "93"
_T_SELSAMPLER = "94"
_T_SIGMAS = "95"
_T_ADV = "96"


def _apply_batch(wf, rec, count):
    """
    把「一次出 N 张」接进工作流（同一个提示词，一次采样出 N 张）。

    * t2i：直接改 EmptyLatentImage.batch_size。
    * edit / bgremove：latent 来自 TextEncodeQwenImage21 第 3 个输出
      （它内部把参考图 VAE 编码好了），**没有 EmptyLatentImage 可改** ——
      必须在它和 KSampler 之间插一个 RepeatLatentBatch，否则改哪里都没用。
    """
    if count <= 1:
        return
    if rec.get("_latent"):
        wf[rec["_latent"]]["inputs"]["batch_size"] = count
        return
    ks = wf[rec["_ks"]]["inputs"]
    src = ks.get("latent_image")
    if not src:
        return
    wf[_BATCH_NODE] = {
        "class_type": "RepeatLatentBatch",
        "inputs": {"samples": src, "amount": count},
    }
    ks["latent_image"] = [_BATCH_NODE, 0]


def _to_turbo(wf, rec, lora_name, strength, nodes):
    """
    把「经典 KSampler 图」机械改写成「viggle-turbo 图」。

    经典图的采样块是单个 KSampler；turbo 需要拆成
    BasicGuider + RandomNoise + KSamplerSelect + ViggleTurboSigmas + SamplerCustomAdvanced，
    并把 LoRA 节点插在 UNET 和 guider 之间。

    不写死节点号：模型源 / latent 源 / 正向条件 / 种子全部从原 KSampler 的输入链接上读，
    所以 t2i（latent 来自 EmptyLatentImage）和 edit / bgremove（latent 来自
    TextEncodeQwenImage21 第 3 个输出）走同一条路径。
    """
    ks_id = rec["_ks"]
    ks = wf[ks_id]["inputs"]
    model_src = list(ks["model"])
    latent_src = list(ks["latent_image"])
    pos_src = list(ks["positive"])
    seed = int(ks.get("seed") or 0)

    wf[_T_LORA] = {
        "class_type": "ViggleTurboLora",
        "inputs": {"model": model_src, "lora_name": lora_name, "strength": strength},
    }
    wf[_T_GUIDER] = {
        "class_type": "BasicGuider",
        "inputs": {"model": [_T_LORA, 0], "conditioning": pos_src},
    }
    wf[_T_NOISE] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf[_T_SELSAMPLER] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": "euler"},
    }
    wf[_T_SIGMAS] = {
        "class_type": "ViggleTurboSigmas",
        "inputs": {"latent": latent_src, "nodes": nodes},
    }
    wf[_T_ADV] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": [_T_NOISE, 0],
            "guider": [_T_GUIDER, 0],
            "sampler": [_T_SELSAMPLER, 0],
            "sigmas": [_T_SIGMAS, 0],
            "latent_image": latent_src,
        },
    }
    dec_id = next(
        (nid for nid, n in wf.items() if n.get("class_type") == "VAEDecode"), None
    )
    if dec_id is None:
        raise ValueError("模板里找不到 VAEDecode，无法切换到加速档")
    wf[dec_id]["inputs"]["samples"] = [_T_ADV, 0]
    del wf[ks_id]


def _to_plain_lora(wf, rec, lora_name, strength):
    """普通 LoRA：合并进权重（LoraLoaderModelOnly），插在模型源和采样器之间。"""
    ks = wf[rec["_ks"]]["inputs"]
    wf[_T_LORA] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": list(ks["model"]), "lora_name": lora_name,
                   "strength_model": strength},
    }
    ks["model"] = [_T_LORA, 0]

# 分辨率预设（长边），Qwen-Image-2.1 是 16 倍下采样
RES_PRESETS = [768, 1024, 1280]

# --------------------------------------------------------------------------
# 模板加载
# --------------------------------------------------------------------------
_TEMPLATES = {}
for _k, _r in RECIPES.items():
    _p = os.path.join(BASE, _r["template"])
    with open(_p, "r", encoding="utf-8") as _f:
        _TEMPLATES[_k] = json.load(_f)
    # 给每个模板定位关键节点（按 class_type 找，不写死 ID）
    _wf = _TEMPLATES[_k]
    _r["_te"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "TextEncodeQwenImage21"),
        None,
    )
    _r["_ks"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "KSampler"), None
    )
    _r["_save"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "SaveImage"), None
    )
    _r["_latent"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "EmptyLatentImage"),
        None,
    )
    _r["_load"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "LoadImage"), None
    )
    # 姿势档没有独立的 TextEncodeQwenImage21 —— 条件编码在 FisherQwenFreePose 内部做
    # （它直接调 TextEncodeQwenImage21.execute，把 image_1=人偶图 / image_2=人物图 一起编进去），
    # KSampler 要的 latent 是它的第 3 个输出，所以这一档没有 EmptyLatentImage 可改。
    _r["_pose"] = next(
        (nid for nid, n in _wf.items() if n.get("class_type") == "FisherQwenFreePose"),
        None,
    )
    if _r["_pose"]:
        assert _r["_ks"] and _r["_load"], f"模板 {_k} 缺关键节点"
    else:
        assert _r["_te"] and _r["_ks"], f"模板 {_k} 缺关键节点"


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------
def http_json(url, payload=None, timeout=30, method=None):
    """请求 JSON 接口，返回 (status, obj)。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    # 本机服务，绕开任何系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body[:400]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def comfy_up():
    st, _ = http_json(COMFY + "/system_stats", timeout=4)
    return st == 200


# --------------------------------------------------------------------------
# 任务登记（内存 + 落盘，重启不丢历史）
# --------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs = {}          # prompt_id -> job dict
_jobs_order = []    # 时间顺序


def _load_jobs():
    global _jobs, _jobs_order
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            _jobs = obj.get("jobs", {})
            _jobs_order = obj.get("order", list(_jobs.keys()))
        except Exception as e:
            print(f"[warn] 历史记录读取失败: {e}")


def _save_jobs():
    try:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"jobs": _jobs, "order": _jobs_order[-300:]}, f, ensure_ascii=False
            )
    except Exception as e:
        print(f"[warn] 历史记录落盘失败: {e}")


def job_add(job):
    with _jobs_lock:
        _jobs[job["id"]] = job
        _jobs_order.append(job["id"])
        _save_jobs()


def job_update(pid, **kw):
    with _jobs_lock:
        if pid in _jobs:
            _jobs[pid].update(kw)
            _save_jobs()


def job_get(pid):
    with _jobs_lock:
        return copy.deepcopy(_jobs.get(pid))


def job_list(limit=60):
    with _jobs_lock:
        ids = [i for i in reversed(_jobs_order) if i in _jobs][:limit]
        return [copy.deepcopy(_jobs[i]) for i in ids]


# --------------------------------------------------------------------------
# 工作流构造
# --------------------------------------------------------------------------
def build_workflow(mode, p):
    """按模式把用户参数注入 API 模板，返回 (workflow, meta)。"""
    rec = RECIPES[mode]
    wf = copy.deepcopy(_TEMPLATES[mode])

    te = wf[rec["_te"]]["inputs"] if rec.get("_te") else None
    ks = wf[rec["_ks"]]["inputs"]

    # 通用参数
    seed = int(p.get("seed") if p.get("seed") not in (None, "", -1) else random.randint(1, 2**31 - 1))
    res = int(p.get("resolution", rec["resolution"]))

    # ---- LoRA ----
    # 姿势档的 LoRA 写死在模板里（VNCCS_QI2_PoseStudioV1.1，strength 1.0），不接受外部指定 ——
    # 换别的 LoRA 会直接破坏「人偶图 image1 → 人物图 image2」的对应关系，画面必崩。
    lora_name = "" if mode == "pose" else (p.get("lora") or "").strip()
    if lora_name and lora_name not in {x["file"] for x in list_loras()}:
        raise ValueError(f"LoRA 不存在：{lora_name}")
    try:
        lora_weight = max(-4.0, min(4.0, float(p.get("lora_weight", 1.0))))
    except (TypeError, ValueError):
        lora_weight = 1.0
    turbo_meta = TURBO_LORAS.get(lora_name)
    # 选了加速 LoRA 就默认开加速档；显式传 turbo=false 可只挂 LoRA 走经典采样
    if turbo_meta is not None:
        turbo = bool(p.get("turbo", True))
    else:
        turbo = bool(p.get("turbo", False)) and bool(lora_name)
        if turbo:
            raise ValueError("加速档只支持 viggle-turbo 系列的 LoRA")

    # 加速档步数：必须是该 LoRA 支持的**官方预设**（sigma 表和步数是一体的，
    # 不能让用户随便填一个数 —— 表里的节点是学生模型训练时的落点）。
    turbo_steps = None
    if turbo:
        allowed = turbo_meta["schedules"]
        raw = p.get("turbo_steps")
        try:
            turbo_steps = int(raw) if raw not in (None, "") else turbo_meta["steps"]
        except (TypeError, ValueError):
            raise ValueError(f"加速档步数必须是整数，收到 {raw!r}")
        if turbo_steps not in allowed:
            raise ValueError(
                f"{turbo_meta['label']} 只支持 "
                f"{'/'.join(str(x) for x in allowed)} 步加速档，收到 {turbo_steps}")

    # 加速档下步数由 sigma 表决定，忽略外部传的 steps（免得 meta 和实际采样步数对不上）
    steps = turbo_steps if turbo else max(1, min(60, int(p.get("steps") or rec["steps"])))
    cfg = max(0.0, min(10.0, float(p.get("cfg", rec["cfg"]))))
    sampler = p.get("sampler") or rec["sampler"]
    scheduler = p.get("scheduler") or rec["scheduler"]
    # 一次出几张（1~4）：同一个提示词，靠 batch 一次采样出 N 张
    try:
        count = max(1, min(MAX_BATCH, int(float(p.get("count") or 1))))
    except (TypeError, ValueError):
        count = 1

    ks["seed"] = seed
    ks["steps"] = steps
    ks["cfg"] = cfg
    ks["sampler_name"] = sampler
    ks["scheduler"] = scheduler

    prompt = (p.get("prompt") or "").strip()
    negative = (p.get("negative") or "").strip()

    # 风格预设：块加在提示词最前面、负向词合并。
    # ⚠️ 抠图档不吃风格 —— 它的 prompt 是固定的 BGREMOVE_PROMPT，套风格块会把抠图指令冲掉。
    style_key, style_label = "", ""
    if mode in ("t2i", "edit"):
        prompt, negative, style_key, style_label = apply_style(p, prompt, negative)

    if mode == "t2i":
        w = int(p.get("width", res))
        h = int(p.get("height", res))
        w -= w % 16
        h -= h % 16
        te["prompt"] = prompt or "a serene landscape, golden hour"
        te["negative_prompt"] = negative
        te["resolution"] = max(w, h)
        wf[rec["_latent"]]["inputs"]["width"] = w
        wf[rec["_latent"]]["inputs"]["height"] = h
        meta = {"size": f"{w}x{h}"}

    elif mode == "edit":
        ref = p.get("ref_image")
        if not ref:
            raise ValueError("参考图编辑需要先上传一张参考图")
        wf[rec["_load"]]["inputs"]["image"] = ref
        te["prompt"] = prompt or "make it a cinematic photo"
        te["negative_prompt"] = negative
        te["resolution"] = res
        meta = {"size": f"跟随参考图 × {res}"}

    elif mode == "pose":
        # 姿势编辑：人偶图（image1）由 pose_json 里的 poseReference 自带，
        # 这里只需要人物参考图（image2）+ 输出尺寸 + 补充提示词。
        ref = p.get("ref_image")
        if not ref:
            raise ValueError("姿势编辑需要先上传一张人物参考图（image2）")
        pose_json = (p.get("pose_json") or "").strip()
        if not pose_json or pose_json in ("{}", "null"):
            raise ValueError("还没有姿势数据 —— 请打开姿势编辑器，摆好姿势后点「应用到节点」")
        try:
            scene = json.loads(pose_json)
        except ValueError as e:
            raise ValueError(f"姿势数据不是合法 JSON：{e}")
        if not str(scene.get("poseReference") or "").startswith("data:image/png;base64,"):
            raise ValueError("姿势数据里没有人偶图（poseReference）—— 请回编辑器重新「应用到节点」")
        w = int(p.get("width", res))
        h = int(p.get("height", res))
        w -= w % 16
        h -= h % 16
        wf[rec["_load"]]["inputs"]["image"] = ref
        pose = wf[rec["_pose"]]["inputs"]
        pose["pose_json"] = pose_json
        pose["extra_prompt"] = prompt
        pose["width"] = w
        pose["height"] = h
        pose["reference_resolution"] = res
        meta = {"size": f"{w}x{h}", "lora": rec.get("fixed_lora")}

    else:  # bgremove
        ref = p.get("ref_image")
        if not ref:
            raise ValueError("背景移除需要先上传一张图片")
        wf[rec["_load"]]["inputs"]["image"] = ref
        te["prompt"] = prompt or BGREMOVE_PROMPT
        te["negative_prompt"] = negative
        te["resolution"] = res
        meta = {"size": f"跟随原图 × {res}"}

    wf[rec["_save"]]["inputs"]["filename_prefix"] = (
        p.get("filename_prefix") or rec["prefix"]
    )
    # 先批量，再改采样块 —— turbo 变换要从 KSampler 上读 latent 源，
    # 而 _apply_batch 对 edit/bgremove 正是靠改 KSampler.latent_image 插 RepeatLatentBatch。
    _apply_batch(wf, rec, count)

    if lora_name:
        if turbo:
            _to_turbo(wf, rec, lora_name, lora_weight, turbo_nodes(turbo_steps))
        else:
            _to_plain_lora(wf, rec, lora_name, lora_weight)

    meta.update({"seed": seed, "steps": steps, "cfg": cfg,
                 "sampler": sampler, "scheduler": scheduler, "resolution": res,
                 "count": count})
    if style_key:
        meta["style"] = style_key
        meta["style_label"] = style_label
    if lora_name:
        meta["lora"] = lora_name
        meta["lora_weight"] = lora_weight
        meta["turbo"] = turbo
        if turbo:
            # 加速档下 cfg / 负向词 / 采样器都不生效，别让它们出现在参数面板里骗人
            meta["sampler"] = "euler"
            meta["scheduler"] = "viggle-turbo sigma"
            meta["cfg"] = None
            meta["turbo_steps"] = turbo_steps
            meta["turbo_nodes"] = turbo_nodes(turbo_steps)
    return wf, meta


def submit(wf):
    """提交给 ComfyUI，返回 (ok, prompt_id_or_error)。"""
    client_id = str(uuid.uuid4())
    st, obj = http_json(COMFY + "/prompt", {"prompt": wf, "client_id": client_id})
    if st == 200 and obj.get("prompt_id"):
        return True, obj["prompt_id"]
    # 把 ComfyUI 的节点校验错误提炼成人话
    msg = obj.get("error") or {}
    detail = ""
    if isinstance(msg, dict):
        detail = msg.get("message") or msg.get("type") or ""
        for k, v in (msg.get("details") or {}).items():
            detail += f" | {k}: {v}"
    if not detail:
        detail = json.dumps(obj, ensure_ascii=False)[:400]
    return False, detail


def fetch_images(pid):
    """从 history 取产物与耗时。返回 None 表示任务还没进 history（仍在跑/排队）。"""
    st, obj = http_json(f"{COMFY}/history/{pid}", timeout=20)
    if st != 200 or pid not in obj:
        return None
    h = obj[pid]
    imgs = []
    for _, out in (h.get("outputs") or {}).items():
        for im in out.get("images", []) or []:
            imgs.append({
                "filename": im.get("filename"),
                "subfolder": im.get("subfolder", ""),
                "type": im.get("type", "output"),
            })
    # 耗时：从 status.messages 里的 execution_start / execution_success 时间戳算
    elapsed = None
    t0 = t1 = None
    err = None
    messages = (h.get("status") or {}).get("messages", []) or []
    for m in messages:
        if not isinstance(m, (list, tuple)) or len(m) < 2:
            continue
        kind, payload = m[0], m[1]
        if kind == "execution_start":
            t0 = (payload or {}).get("timestamp")
        elif kind == "execution_success":
            t1 = (payload or {}).get("timestamp")
        elif kind == "execution_error":
            p = payload or {}
            err = (f"{p.get('node_type', '?')}: "
                   f"{p.get('exception_message') or p.get('exception_type') or '执行出错'}")
    if t0 and t1:
        elapsed = round((t1 - t0) / 1000.0, 1)
    return {"images": imgs, "elapsed": elapsed, "error": err}


def queue_state(pid):
    """返回 'running' / 'pending' / None 及队列位置。"""
    st, obj = http_json(COMFY + "/queue", timeout=10)
    if st != 200:
        return None, None
    for i, item in enumerate(obj.get("queue_pending") or []):
        if len(item) > 1 and item[1] == pid:
            return "pending", i + 1
    for item in obj.get("queue_running") or []:
        if len(item) > 1 and item[1] == pid:
            return "running", 0
    return None, None


# --------------------------------------------------------------------------
# 提示词扩写：见 _prompt_eng.py
# --------------------------------------------------------------------------
# 官方 PE-T2I（Qwen3.5-VL-9B 微调，Q4_K_M 5.24 GiB）经 Ollama 加载后，
# `_prompt_eng.enhance()` 会走官方八步法 system prompt；
# 未装 PE 则回落 huihui_ai/qwen2.5-abliterate:7b-instruct + 压缩版 prompt。
# 两条路都是 num_gpu=0 + keep_alive=0 —— 纯 CPU、用完即卸，不抢那 8 GB 显存。


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Qwen21Web/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 响应工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None,
              cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 浏览器关标签页 / 取消请求 / 轮询超时都会走到这里（WinError 10053 / 10054）。
            # 这不是错误，静默丢弃即可 —— 否则每个断开的请求都会刷一屏 traceback。
            pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        # 静音轮询噪音：前端每 6 秒轮一次 health + queue，状态轮询更密。
        # 不静音的话日志一天几万行，真出问题时翻不出来。
        p = self.path or ""
        if "/api/status" in p or "/api/health" in p or "/api/queue" in p:
            return
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    # ---- 路由 ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path.startswith("/fisher/"):
            return self._serve_fisher(path[len("/fisher/"):])
        # Fisher 编辑器要用的 ComfyUI 原生接口（内置骨架库 / 我的图库 / 我的姿势 /
        # 图片查看 / 上传），原样转发 —— 必须在同一个源上，编辑器才会认。
        if path.startswith("/fisher_pose/") or path == "/view":
            return self._proxy_raw("GET")
        if path == "/api/config":
            return self._api_config()
        if path == "/api/status":
            return self._api_status(qs.get("id", [""])[0])
        if path == "/api/jobs":
            return self._send(200, {"jobs": job_list()})
        if path == "/api/queue":
            st, obj = http_json(COMFY + "/queue", timeout=10)
            return self._send(200, obj if st == 200 else {"error": obj})
        if path == "/image":
            return self._proxy_view(qs)
        if path == "/api/health":
            return self._send(200, {
                "comfy": comfy_up(),
                "ollama": http_json(OLLAMA + "/api/tags", timeout=3)[0] == 200,
                "model": PE.active_model(),
                "pe": PE.pe_available(),
                "pe_model": PE.PE_MODEL,
                "fallback_model": PE.FALLBACK_MODEL,
            })
        if path == "/api/lexicon":
            return self._send(200, PE.lexicon_payload())
        if path == "/api/loras":
            return self._send(200, {"loras": list_loras()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            if path == "/api/generate":
                return self._api_generate()
            if path == "/api/upload":
                return self._api_upload()
            if path == "/api/enhance":
                return self._api_enhance()
            if path == "/api/free":
                st, obj = http_json(COMFY + "/free",
                                    {"unload_models": True, "free_memory": True})
                return self._send(200, {"ok": st == 200, "detail": obj})
            if path == "/api/interrupt":
                st, obj = http_json(COMFY + "/interrupt", {})
                return self._send(200, {"ok": st == 200, "detail": obj})
            if path == "/api/clear-jobs":
                with _jobs_lock:
                    _jobs.clear()
                    _jobs_order.clear()
                    _save_jobs()
                return self._send(200, {"ok": True})
            # 编辑器往「我的图库」上传骨架图、保存姿势都走这里
            if path.startswith("/fisher_pose/") or path == "/upload/image":
                return self._proxy_raw("POST")
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    # ---- 静态文件 ----
    def _serve_static(self, rel):
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel:
            return self._send(403, {"error": "forbidden"})
        fp = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(fp):
            return self._send(404, {"error": f"missing {rel}"})
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        }.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
        with open(fp, "rb") as f:
            return self._send(200, f.read(), ctype)

    # ---- Fisher Pose 编辑器静态资源 ----
    def _serve_fisher(self, rel):
        """
        代理 Fisher Pose 插件的 web/ 目录（给「姿势编辑」Tab 的 iframe 用）。

        ⚠️ `.mjs` 必须给 application/javascript，否则浏览器按 nosniff 拒绝执行 ES 模块；
        ⚠️ 86 MB 的 MakeHuman 人体包要**允许浏览器缓存** —— 默认的 no-store 会让每次
           打开编辑器都重下 86 MB（表现为「正在加载 MakeHuman 人体」卡很久）。
        """
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel:
            return self._send(403, {"error": "forbidden"})
        fp = os.path.normpath(os.path.join(FISHER_WEB, rel))
        if not (fp == FISHER_WEB or fp.startswith(FISHER_WEB + os.sep)):
            return self._send(403, {"error": "forbidden"})
        if os.path.isdir(fp):
            fp = os.path.join(fp, "index.html")
        if not os.path.isfile(fp):
            return self._send(404, {"error": f"missing {rel}"})
        ext = os.path.splitext(fp)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".bin": "application/octet-stream", ".wasm": "application/wasm",
            ".woff2": "font/woff2",
        }.get(ext, "application/octet-stream")
        # 人体包这类大件长缓存；html / mjs 短缓存，改完插件刷新页面就生效
        cache = "public, max-age=604800" if ext in (".bin", ".woff2") else "no-cache"
        with open(fp, "rb") as f:
            return self._send(200, f.read(), ctype, cache=cache)

    # ---- 原样转发给 ComfyUI（同源代理，编辑器握手才成立）----
    def _proxy_raw(self, method="GET"):
        url = COMFY + (self.path or "/")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        ctype_in = self.headers.get("Content-Type")
        if ctype_in:
            req.add_header("Content-Type", ctype_in)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=180) as r:
                return self._send(200, r.read(),
                                  r.headers.get("Content-Type") or "application/octet-stream",
                                  cache="no-cache")
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read(),
                              e.headers.get("Content-Type") or "application/json; charset=utf-8")
        except Exception as e:
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})

    # ---- 配置 ----
    def _api_config(self):
        return self._send(200, {
            "recipes": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                        for k, v in RECIPES.items()},
            "res_presets": RES_PRESETS,
            "bgremove_prompt": BGREMOVE_PROMPT,
            "loras": list_loras(),
            "turbo_schedules": {str(k): {"nodes": v[0], "label": v[1], "note": v[2]}
                                for k, v in TURBO_SCHEDULES.items()},
            "styles": style_list(),
            "style_order": STYLE_ORDER,
            "lan_url": lan_url(),
            "comfy_url": COMFY,
            "port": PORT,
        })

    # ---- 生成 ----
    def _api_generate(self):
        p = self._json_body()
        mode = p.get("mode") or "t2i"
        if mode not in RECIPES:
            return self._send(400, {"error": f"未知模式 {mode}"})
        if not comfy_up():
            return self._send(503, {"error": "ComfyUI 未响应，请先启动（启动ComfyUI.bat）"})
        try:
            wf, meta = build_workflow(mode, p)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        ok, res = submit(wf)
        if not ok:
            return self._send(400, {"error": res})
        pid = res
        job = {
            "id": pid,
            "mode": mode,
            "mode_label": RECIPES[mode]["label"],
            "prompt": p.get("prompt", ""),
            "negative": p.get("negative", ""),
            "params": meta,
            "created": time.time(),
            "state": "queued",
            "elapsed": None,
            "images": [],
        }
        job_add(job)
        return self._send(200, {"id": pid, "job": job})

    def _api_status(self, pid):
        if not pid:
            return self._send(400, {"error": "缺少 id"})
        job = job_get(pid)
        got = fetch_images(pid)
        if got is not None:
            # history 里有记录 ≠ 成功：中断 / 报错会留下「0 产物」的空记录
            if got["images"]:
                state = "done"
            elif got.get("error"):
                state = "error"
            else:
                state = "interrupted"
            job = job or {"id": pid}
            job.update(images=got["images"], elapsed=got["elapsed"], state=state,
                       error=got.get("error"))
            job_update(pid, images=got["images"], elapsed=got["elapsed"],
                       state=state, error=got.get("error"))
            return self._send(200, {"state": state, "job": job})
        state, pos = queue_state(pid)
        if job:
            job["state"] = state or "unknown"
        return self._send(200, {"state": state or "unknown",
                                "queue_position": pos, "job": job})

    # ---- 上传（前端传 base64，服务端转 multipart 转发给 ComfyUI）----
    def _api_upload(self):
        p = self._json_body()
        name = os.path.basename(p.get("filename") or "upload.png")
        b64 = p.get("data") or ""
        if not b64:
            return self._send(400, {"error": "缺少文件数据"})
        import base64
        try:
            raw = base64.b64decode(b64.split(",")[-1])
        except Exception as e:
            return self._send(400, {"error": f"base64 解码失败: {e}"})
        if len(raw) > 32 * 1024 * 1024:
            return self._send(400, {"error": "图片过大（>32MB）"})

        boundary = "----Qwen21Web" + uuid.uuid4().hex
        body = b""
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="image"; '
                 f'filename="{name}"\r\n').encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += raw + b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            COMFY + "/upload/image", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            return self._send(500, {"error": f"上传到 ComfyUI 失败: {e}"})

        sub = obj.get("subfolder") or ""
        obj["ref"] = f"{sub}/{obj['name']}" if sub else obj["name"]
        return self._send(200, obj)

    # ---- 提示词扩写（维度词库 + 多方向）----
    def _api_enhance(self):
        p = self._json_body()
        text = (p.get("prompt") or "").strip()
        mode = p.get("mode") or "t2i"
        if mode not in RECIPES:
            mode = "t2i"
        lang = "zh" if p.get("lang") == "zh" else "en"
        try:
            count = max(1, min(3, int(p.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        if mode == "bgremove":
            return self._send(200, {
                "prompts": [{"direction": "single", "label": "固定指令",
                             "text": BGREMOVE_PROMPT}],
                "source": "fixed", "source_label": "固定指令",
                "count": 1, "lang": lang})
        if not text:
            return self._send(400, {"error": "提示词为空"})
        if p.get("offline"):
            return self._send(200, {
                "prompts": PE.offline_enhance(text, mode, count),
                "source": "offline", "source_label": "离线结构增强",
                "count": count, "lang": lang})
        try:
            out, meta = PE.enhance(text, mode=mode, count=count, lang=lang)
            src = meta.get("source")
            label = ("官方 PE-T2I · " + str(meta.get("model"))) if src == "pe" \
                else "通用模型 · " + str(meta.get("model"))
            if meta.get("wh_ratio"):
                label += " ｜ 建议画幅 " + str(meta["wh_ratio"])
            return self._send(200, {
                "prompts": out, "source": src, "source_label": label,
                "model": meta.get("model"), "wh_ratio": meta.get("wh_ratio"),
                "count": len(out), "lang": lang,
                # 段数不足等异常由 PE 侧给出说明，别吞掉
                "note": meta.get("note")})
        except Exception as e:
            return self._send(200, {
                "prompts": PE.offline_enhance(text, mode, count),
                "source": "offline", "source_label": "离线结构增强",
                "count": count, "lang": lang,
                "note": "Ollama 不可用（%s），已用离线结构增强" % e})

    # ---- 代理 ComfyUI /view ----
    def _proxy_view(self, qs):
        q = {
            "filename": (qs.get("filename") or [""])[0],
            "subfolder": (qs.get("subfolder") or [""])[0],
            "type": (qs.get("type") or ["output"])[0],
        }
        url = COMFY + "/view?" + urllib.parse.urlencode(q)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=60) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "image/png")
        except Exception as e:
            return self._send(404, {"error": f"取图失败: {e}"})
        return self._send(200, data, ctype, {"Content-Disposition": "inline"})


# 虚拟网卡关键词（命中就跳过）。用品牌 / 技术名而不是 ipconfig 的字段名 ——
# 字段名会随系统语言变（中文系统上是「IPv4 地址」「默认网关」），品牌名不会。
_VIRTUAL_HINTS = (
    "vmware", "virtualbox", "hyper-v", "vethernet", "wsl", "docker",
    "tailscale", "zerotier", "wireguard", "openvpn", "tap-", "tunnel",
    "loopback", "bluetooth", "npcap", "miniport", "vpn",
    "回环", "蓝牙", "隧道",
)
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _ip_rank(ip):
    """
    IPv4 的「像不像同一个局域网里的地址」打分，越小越好；-1 = 不可用。

    关键是把 **CGNAT 100.64.0.0/10** 判掉 —— Tailscale 就是从这个段里发地址的，
    它排在默认路由最前面，正是老写法会拿错的那个。
    """
    try:
        a, b, c, d = (int(x) for x in ip.split("."))
        if not all(0 <= v <= 255 for v in (a, b, c, d)):
            return -1
    except Exception:
        return -1
    if a in (0, 127) or a >= 224:
        return -1
    if a == 169 and b == 254:            # link-local
        return -1
    if a == 100 and 64 <= b <= 127:      # CGNAT（Tailscale 等）
        return -1
    if a == 192 and b == 168:
        return 0                          # 家用 / 小型办公网，最常见
    if a == 10:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    return 3                              # 公网段，实在没别的才用


def _lan_ip_from_ipconfig():
    """解析 `ipconfig`，挑出**真正连着局域网**的那张网卡。

    判据：**只有真网卡才有默认网关**。本机实测的对照表 ——

    | 适配器 | IPv4 | 默认网关 |
    |---|---|---|
    | Wireless LAN adapter Wi-Fi | 192.168.1.23 | 192.168.1.1 ← 要的就是它 |
    | Ethernet adapter Ethernet 3（Tailscale）| 100.64.0.5 | 0.0.0.0 |
    | vEthernet (Default Switch) | 172.26.128.1 | 空 |
    | vEthernet (WSL) | 172.18.240.1 | 空 |
    | VMware VMnet1 / VMnet8 | 192.168.209.1 / 192.168.48.1 | 空 |

    块内字段顺序固定是 IPv4 地址 → 子网掩码 → 默认网关，所以「块里第 3 个以后的
    IPv4 字面量」就是网关。只认值不认字段名，中英文系统都能跑。
    """
    import subprocess as _sp
    out = _sp.run(["ipconfig"], capture_output=True, timeout=8).stdout
    txt = None
    for enc in ("gbk", "utf-8", "cp936"):
        try:
            txt = out.decode(enc)
            break
        except Exception:
            continue
    if txt is None:
        txt = out.decode("gbk", "replace")

    blocks, cur = [], None
    for line in txt.splitlines():
        s = line.strip()
        if not s:
            continue
        # 适配器标题行：以冒号结尾，且不含 ipconfig 的「 . . . 」点线
        if s.endswith(":") and " . " not in s:
            cur = {"head": s, "ips": []}
            blocks.append(cur)
            continue
        if cur is not None:
            cur["ips"] += _IPV4_RE.findall(s)

    best, best_rank = "", 99
    for b in blocks:
        if any(h in b["head"].lower() for h in _VIRTUAL_HINTS):
            continue
        ips = b["ips"]
        if len(ips) < 3:                       # 没有网关（虚拟网卡都是这样）
            continue
        addr, gw = ips[0], ips[-1]
        if gw == "0.0.0.0" or _ip_rank(gw) == -1:
            continue
        r = _ip_rank(addr)
        if r >= 0 and r < best_rank:
            best, best_rank = addr, r
    return best


def lan_ip():
    """
    取本机在**同一局域网**里的 IPv4（手机 / 平板该访问的那个）。取不到返回空串。

    ⚠️ **不要用「UDP connect 8.8.8.8 再 getsockname」那个常见写法。** 它拿的是
    **默认路由**那张网卡；装了 Tailscale / VPN 之后默认路由被它们抢走，
    拿到的是 100.64/10 的 CGNAT 地址（本机实测 100.64.0.5）——
    网页上会显示一个手机根本连不上的地址。改用 ipconfig + 默认网关判定。
    """
    try:
        ip = _lan_ip_from_ipconfig()
        if ip:
            return ip
    except Exception:
        pass

    # 兜底：老办法，但把 CGNAT 判掉（那正是 Tailscale 会给的地址）
    import socket as _s
    for probe in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        try:
            sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            sk.settimeout(0.3)
            sk.connect((probe, 80))
            ip = sk.getsockname()[0]
            sk.close()
            if _ip_rank(ip) >= 0:
                return ip
        except Exception:
            continue
    try:
        cands = [( _ip_rank(i[4][0]), i[4][0])
                 for i in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET)]
        cands = [(r, ip) for r, ip in cands if r >= 0]
        if cands:
            return min(cands)[1]
    except Exception:
        pass
    return ""


def lan_url():
    """
    手机 / 平板该访问的地址。只在监听 0.0.0.0 时才有意义（仅本机模式返回空串）。

    前端头部会把它显示成一个可点击复制的胶囊 —— 否则用户只能去翻控制台窗口，
    而那个窗口一回车就关了。
    """
    if HOST in ("127.0.0.1", "localhost"):
        return ""
    ip = lan_ip()
    return f"http://{ip}:{PORT}" if ip else ""


class QuietServer(ThreadingHTTPServer):
    """
    浏览器很爱干两件事，都会让默认实现刷一屏 traceback：

    * **预连接**（preconnect / speculative connection）：连上就断，不發请求 ——
      在 `handle_one_request` 的 `rfile.readline()` 抛 `ConnectionAbortedError`(10053)。
    * **中途取消**：请求发一半或响应读一半就断 —— 在 `wfile.write()` 抛。

    这两类都是 `ConnectionError` 的子类，**不是错误**，静默丢弃即可。
    （WinError 10053 = 本机软件中止了连接，10054 = 对端重置。）
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    _load_jobs()
    if not os.path.isdir(WEB_DIR):
        os.makedirs(WEB_DIR, exist_ok=True)
    print("=" * 62)
    print("  Qwen-Image-2.1 本地 Web 前端")
    print("=" * 62)
    print(f"  ComfyUI 后端 : {COMFY}  ({'在线' if comfy_up() else '未响应'})")
    _oll = http_json(OLLAMA + "/api/tags", timeout=3)[0] == 200
    print(f"  Ollama       : {OLLAMA}  ({'在线' if _oll else '未启动，将用离线增强'})")
    if _oll:
        _pe = PE.pe_available(force=True)
        print(f"  提示词扩写   : {'官方 PE-T2I · ' + PE.PE_MODEL if _pe else '通用模型 · ' + PE.FALLBACK_MODEL}"
              + ("   [纯 CPU]" if PE.PE_NUM_GPU == 0 else ""))
        if not _pe:
            print(f"                 （想用官方 PE：ollama create {PE.PE_MODEL} "
                  f"-f models\\prompt_enhancer\\Modelfile）")
    print(f"  打开浏览器   : http://127.0.0.1:{PORT}")
    _lan_file = os.path.join(BASE, "_lan_url.txt")
    if HOST not in ("127.0.0.1", "localhost"):
        _ip = lan_ip()
        print(f"  局域网访问   : http://{_ip or '<本机IP>'}:{PORT}"
              "   （同一 WiFi 下的手机/平板）")
        try:  # 供 .bat 读取后回显给用户
            with open(_lan_file, "w", encoding="ascii", errors="replace") as f:
                f.write(f"http://{_ip}:{PORT}" if _ip else "")
        except Exception:
            pass
    else:
        print("  局域网访问   : 未开启（设 WEB_HOST=0.0.0.0 才允许局域网）")
        try:
            os.remove(_lan_file)
        except Exception:
            pass
    print("  停止服务     : Ctrl+C")
    print("=" * 62)
    srv = QuietServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
