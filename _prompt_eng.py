#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_prompt_eng.py —— 提示词工程模块（出图台的「提示词助手」后端）

参考来源（2026-09-26 调研）
--------------------------
1. **Qwen 官方 PE-T2I system prompt**（`Qwen/Qwen-Image-2.1-PE-T2I`）—— 八步法：
   拆 brief → 定画幅 → 开场句 → 清点元素 → 走遍画面 → 设定文字 → 光线单句 → 整幅收尾。
   官方硬性要求：**约 400-500 词**、观察式而非指令式、**禁止质量词**
   （no masterpiece / 8K / highly detailed / award-winning）、不确定处要 hedge、
   颜色带修饰词、给材质而非名词、枚举不概括。
2. **FLUX.2 提示词指南** —— 七阶段顺序（Scene→Subject→Action→Lighting→Camera→Style→Mood）、
   注意力有位置偏置（**承重元素前置**）、显式对抗中心偏置与浅景深偏置。
3. **官方 Edit 的 Attribute Disentanglement** —— IDENTITY→CHANGE→SOURCE→PRESERVE、
   `<image1>` 标签语法、blanket preservation clause、"不要重新描述脸"。

本机实测（2026-09-26，RTX 2000 Ada 8 GB / Core Ultra 7 165H / 63 GB RAM）
--------------------------------------------------------------------------
| 路线                | 墙钟   | 其中加载 | 预填      | 生成          | 质量 |
|---------------------|--------|----------|-----------|---------------|------|
| PE-T2I 9B Q4_K_M CPU| 193 s  | 117.8 s  | 13.2 s    | 62.3 s(4.2t/s)| 好   |
| 通用 7B Q4_K_M  CPU | 35 s   | 11.3 s   | 10.8 s    | 13.3 s        | 差   |

结论与对策：
* **PE 质量明显更好**（位置短语、材质、颜色修饰词都到位，是官方八步法的风格）；
  通用 7B 会提前停（63 token 就收），位置还自相矛盾。所以有 PE 就优先用 PE。
* **PE 上不了显卡**：6.2 GB 权重 + 8 GB 卡，实测 `num_gpu=99` 时卡上只剩 855 MiB
  可用 → 直接 HTTP 500（OOM）。所以固定走 CPU（`PE_NUM_GPU=0`）。
* **官方那份 9907 字符的 system prompt 本机用不起**：约 2400 token，
  纯 CPU 预填 ~190 秒。默认改用 `SYS_CORE`（~200 token，同一套八步法的压缩版）。
  想恢复官方原文：`PE_FULL_PROMPT=1`（只在有 16 GB+ 卡时值得）。
* **冷加载 117.8 秒是最大浪费**，所以 `PE_KEEP_ALIVE` 默认 30 分钟常驻 ——
  第二次起只要「预填 + 生成」≈ 75 秒。CPU 推理只占内存，不碰显存。
* 长度仍做了妥协：官方要 400-500 词，本机默认 `short`（~150 词）。
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
PE_MODEL = os.environ.get("PE_MODEL", "qwen-image-pe-t2i")
FALLBACK_MODEL = os.environ.get(
    "OLLAMA_MODEL", "huihui_ai/qwen2.5-abliterate:7b-instruct")

# 走 GPU 的层数。
# ⚠️ 实测（2026-09-26，8 GB RTX 2000 Ada）：
#   * PE-T2I Q4_K_M 5.24 GiB，卡上只剩 ~855 MiB 可用时 num_gpu=99 直接 HTTP 500（OOM）。
#   * 纯 CPU 下 PE 生成 3.86~4.2 tok/s —— 能跑，一段 120 词描述约 110 秒。
#   * 卡空着时上 GPU 快一个数量级；卡被占时若放任 Ollama「部分卸载」，
#     反而比纯 CPU 还慢（实测只剩 879 MiB 空闲，8 分钟跑不完一次）。
# ⇒ 默认 auto：每次调用前看空闲显存，够就上卡（-1 交给 Ollama 全量放置），
#   不够就老实用 CPU。用完 keep_alive=0 立刻卸载，不会长期占卡。
#   想强制纯 CPU（比如正开着别的吃显存程序）：设 PE_NUM_GPU=0。
PE_NUM_GPU = os.environ.get("PE_NUM_GPU", "auto")  # auto | -1 | 0 | 具体层数
# auto 门槛（MiB）：权重 5366 + KV(4096 ctx) ~650 + CUDA 开销 ~300 + 余量
PE_VRAM_NEED = int(os.environ.get("PE_VRAM_NEED", "6700"))
# 兜底模型（7B Q4 4.7 GB）比 PE 小，显存空的时候也能上卡。
FB_NUM_GPU = int(os.environ.get("OLLAMA_NUM_GPU", "0"))

# 用完保持多久。默认 auto —— **按设备分别处理**，别一刀切：
#   * 上 GPU → 立刻卸载（keep_alive=0）。8 GB 卡只有一块，PE 占着 5.24 GiB
#     就等于把 ComfyUI 挡在门外，必须一用完就还回去。
#   * 纯 CPU → 常驻 30 分钟。实测冷加载要 117.8 秒（5.6 GB GGUF），
#     每次扩写都重载是纯浪费；CPU 推理只占系统内存（6.2 GB / 本机 63 GB），不碰显存。
# 想强制：PE_KEEP_ALIVE=0（总是立刻卸）/ PE_KEEP_ALIVE=30m（总是常驻）。
PE_KEEP_ALIVE = os.environ.get("PE_KEEP_ALIVE", "auto")
PE_KEEP_ALIVE_CPU = os.environ.get("PE_KEEP_ALIVE_CPU", "30m")

# 是否用官方那份 9907 字符的长 system prompt。
# 实测：它约 2400 token，纯 CPU 预填就要 ~190 秒，本机不划算，默认用压缩版。
PE_FULL_PROMPT = os.environ.get("PE_FULL_PROMPT", "0") == "1"

# 单次请求超时（秒）。3 方向 + 长文在 CPU 上可能跑 3 分钟，给足。
PE_TIMEOUT = int(os.environ.get("PE_TIMEOUT", "900"))

# --------------------------------------------------------------------------
# 词库 —— 前端「维度标签」用，也用于离线兜底
# 来源：官方 PE 的媒介词表 + FLUX.2 指南的镜头/光线/胶片词表（已剔掉质量词）
# --------------------------------------------------------------------------
LEXICON = {
    "ratio": {
        "label": "画幅",
        "items": ["1:1", "3:2", "2:3", "16:9", "9:16", "4:5"],
    },
    "medium": {
        "label": "媒介",
        "items": [
            "realistic photograph", "photorealistic", "cinematic still",
            "editorial fashion photo", "documentary photo",
            "flat vector illustration", "watercolour", "oil painting",
            "3D render", "isometric illustration", "line art",
            "poster design", "infographic",
        ],
    },
    "lens": {
        "label": "镜头",
        "items": [
            "85mm f/1.4", "50mm f/1.8", "35mm f/2.0", "24mm wide-angle",
            "14mm ultra-wide", "100mm macro", "135mm telephoto",
            "anamorphic 40mm", "fisheye", "tilt-shift",
        ],
    },
    "shot": {
        "label": "景别",
        "items": [
            "extreme close-up", "close-up", "medium shot", "full body shot",
            "wide establishing shot", "over-the-shoulder",
            "low angle", "high angle", "bird's-eye view", "Dutch angle",
        ],
    },
    "light": {
        "label": "光线",
        "items": [
            "golden hour backlight", "blue hour", "soft window light from the left",
            "overcast diffuse light", "hard overhead studio light",
            "Rembrandt lighting", "rim light from behind", "chiaroscuro",
            "neon signage glow", "single practical streetlamp", "god rays",
            "candlelight", "low-key", "high-key",
        ],
    },
    "film": {
        "label": "胶片/调色",
        "items": [
            "Kodak Portra 400", "Kodak Ektachrome", "Cinestill 800T",
            "Kodak Vision3 500T", "Ilford HP5", "Fujifilm Superia",
            "teal-and-orange grade", "crushed blacks", "heavy halation",
            "muted earth tones", "high-contrast monochrome",
        ],
    },
    "composition": {
        "label": "构图",
        "items": [
            "rule of thirds, subject on the left vertical",
            "subject framed on the far right edge",
            "centred symmetrical composition",
            "negative space dominating the right two-thirds",
            "foreground / midground / background layering",
            "deep focus from foreground to background",
            "shallow depth of field, creamy bokeh",
        ],
    },
    "mood": {
        "label": "氛围",
        "items": [
            "serene and contemplative", "moody and intimate", "slow dread",
            "nostalgic and warm", "energetic and vibrant", "melancholic",
            "clinical and sterile", "dreamlike", "quiet solitude",
        ],
    },
    "material": {
        "label": "材质",
        "items": [
            "brushed metal", "matte plastic", "glossy ceramic", "coarse linen",
            "weathered wood", "frosted glass", "raw concrete", "aged brass",
            "visible brush strokes", "condensation on glass",
        ],
    },
    # ---- 抓拍 / 偷拍视角的摄影语言（2026-09-28 收录）----
    # 来源：X @VoxcatAI 的「偷拍视角角色摄影」提示词模板。
    # 只取其中的**摄影语言**（机位 / 前景遮挡 / 不完美构图 / 被抓包瞬间）——
    # 这部分是通用抓拍摄影词汇，跟「街拍 / candid photography」同源，和题材无关。
    # 安全边界写在 STYLE_PRESETS 的模板里（成年角色 / 公共空间 / 虚构摆拍 / 服装完整）。
    "candid_cam": {
        "label": "抓拍机位",
        "items": [
            "shot from behind a door frame, subject half hidden",
            "shot from behind a wall corner",
            "shot from behind a pillar, subject partially blocked",
            "shot through a narrow gap between shelves",
            "shot through plant leaves in the foreground",
            "shot past the edge of a table, low camera position",
            "shot from behind a chair back",
            "shot through a car window frame",
            "long distance telephoto candid shot, compressed perspective",
            "handheld phone snapshot, slightly crooked framing",
            "camera held at waist height, looking slightly up",
            "shot through a slit, heavy dark edges on both sides",
            "seen indirectly in a shop window reflection",
            "seen in cafe glass, reflection ghosting and glare",
        ],
    },
    "obstruction": {
        "label": "前景遮挡",
        "items": [
            "a door frame takes up the left third of the frame",
            "a wall corner blocks the right edge of the frame",
            "a concrete pillar cuts across the subject",
            "shelf edges frame the subject through a narrow slit",
            "out-of-focus plant leaves fill the near foreground",
            "the rim of a coffee cup sits in the foreground",
            "a table edge runs across the bottom of the frame",
            "the back of a chair blurs across the lower frame",
            "a car window frame vignettes the shot",
            "an escalator railing crosses the foreground",
            "a foreground object covers about a third of the frame",
        ],
    },
    "snapshot": {
        "label": "抓拍质感",
        "items": [
            "imperfect framing, subject off centre",
            "horizon slightly tilted, handheld feel",
            "subject partially cropped at the frame edge",
            "a large empty area on one side of the frame",
            "auto exposure look, highlights slightly blown out",
            "computational phone camera texture",
            "light digital noise and slight motion blur",
            "focus slightly missed, shallow depth of field",
            "glass reflection ghosting over the image",
            "spontaneous candid snapshot",
            "caught mid action, no posed stance",
        ],
    },
    "noticed": {
        "label": "被抓包瞬间",
        "items": [
            "the subject has just noticed the camera, glancing sideways at the lens",
            "the subject frowns slightly, realising someone is photographing them",
            "the subject stops mid action and turns their head toward the camera",
            "the subject stares coldly into the lens",
            "a puzzled look, as if sensing the photographer",
        ],
    },
}

# --------------------------------------------------------------------------
# 风格模板 —— 前端「模板」按钮一键套用
# 来源：X @VoxcatAI 的「偷拍视角角色摄影」提示词模板（2026-09-28 收录）
#
# ⚠️ 收录时做的处理：
#   1. 只保留**摄影语言**，砍掉「色狼视角」这类角色扮演设定（那是给 LLM 的人设，
#      对图像模型没用，只会污染 prompt）。
#   2. 把安全边界**钉死在模板正文里**：成年角色 / 公共或半公共场所 / 虚构摆拍 /
#      服装完整穿着 / 排除私密空间（厕所、更衣室、浴室、卧室）/ 排除未成年人。
#      这跟原模板自己的声明一致（原文也写了「虚构、摆拍、电影化模拟」）。
#   3. `{subject}` 会被前端替换成用户在提示框里已写的内容。
# --------------------------------------------------------------------------
STYLE_PRESETS = [
    {
        "key": "candid",
        "label": "抓拍视角",
        "desc": "前景遮挡 + 长焦压缩 + 不完美构图的公共空间抓拍（成年角色，虚构摆拍）",
        # 模板正文写了「服装完整穿着」，但本模型实测会**自动性感化漂移**
        # （写 modest 无效、white linen shirt → 衬衫+裸腿），默认 negative 又是空的，
        # 所以这里主动给一条压制词。前端只在负向框为空时填，用户可自行清掉。
        "negative": "cleavage, revealing clothing, low cut, exposed midriff, high slit, "
                    "lingerie, seductive pose, exaggerated curves, wet clothing, nsfw",
        "text": (
            "{subject}，明确成年角色，高质量真人 COS 抓拍感角色摄影。"
            "保留角色标志性的发型、发色、五官特征、代表性配色、服装结构与饰品，"
            "不要廉价假发、塑料感服装或明显 AI cosplay 感。服装完整穿着。\n\n"
            "角色正在公共日常环境中自然活动，没有正式摆拍：低头看手机、整理头发、喝饮料、"
            "看橱窗、等车、走路或短暂停留，动作是真实生活中的中间状态，"
            "根据角色性格自动选择最自然的动作。\n\n"
            "摄影机像躲在附近偷偷抓拍，从前景障碍物后方观察人物：门框、墙角、柱子、"
            "货架缝隙、植物叶片、咖啡杯、桌角或椅背后，前景障碍物占画面 15%~40%。"
            "人物偏离画面中心，构图略显仓促、不完全对称，允许轻微裁切与倾斜，一侧留大量空白。"
            "镜头 35~200mm：近距离用手机随手抓拍感（构图略歪、自动曝光、高光轻微过曝、"
            "轻微数字噪点），远距离用长焦压缩空间、背景轻微虚化。"
            "浅景深，前景明显虚化，人物面部保持可识别。\n\n"
            "只用真实环境光：商场冷白灯、咖啡店暖色灯、阴天散射光、傍晚自然光、"
            "便利店荧光灯或地铁人工照明，不要棚拍主光，人物要像真实存在于环境之中。\n\n"
            "真实皮肤纹理，自然发丝，真实织物，合理人体比例，自然手部结构，"
            "轻微运动模糊、对焦误差或玻璃反光，不做过度磨皮、过度 HDR 或过度电影调色。\n\n"
            "spontaneous candid photography, telephoto candid shot, foreground obstruction, "
            "imperfect framing, compressed perspective, partially obscured subject.\n\n"
            "（约 20%~30% 的画面可以让角色突然侧眼或回头看向镜头，形成「发现有人在拍」的瞬间；"
            "不要夸张惊吓表情。整体为虚构摆拍摄影场景。）"
        ),
    },
]

# --------------------------------------------------------------------------
# 三个方向 —— 一次给用户 3 个不同取向的版本
# --------------------------------------------------------------------------
DIRECTIONS = [
    {"key": "photo", "label": "写实摄影",
     "hint": "a realistic photograph, photographic vocabulary, natural optics"},
    {"key": "cinema", "label": "电影感",
     "hint": "a cinematic film still, anamorphic framing, dramatic lighting, "
             "film-grade colour"},
    {"key": "art", "label": "插画 / 平面",
     "hint": "a graphic illustration or poster, flat shapes, strong graphic "
             "composition, print-friendly"},
]

# --------------------------------------------------------------------------
# System prompt —— 官方八步法的压缩版
# --------------------------------------------------------------------------
SYS_CORE = """You describe finished images. You are an observer reporting what is in the frame, \
not an assistant talking to a user and not an instructor talking to a renderer.

Follow this order:
1. Open with ONE sentence naming the medium, the style, the subject, and the background or palette.
2. Place every element with a positional phrase - upper-left, across the top, on the far right, \
in the lower-third, in front of, behind. Use 6 to 10 such phrases and let them reach the corners, \
the edges and the centre, not cluster in the middle.
3. Give the lighting its own sentence: the source, its direction, its quality, and the shadows and \
highlights it leaves.
4. End with ONE sentence covering composition, palette, style and mood.

Hard rules:
- The subject the user names must appear EXACTLY as named. If the request is in another \
language, translate the subject faithfully first - never substitute a similar or more familiar \
thing. "一只柯基" is a corgi (a dog), not a person. If the user asks for an animal doing a human \
action, describe the animal doing it.
- Describe ONLY what the user asked for plus what you must invent to fill the frame. Never add \
narrative you were not given: no backstory, no implied social condition, no extra people.
- Present tense, third person, declarative. One single paragraph, no blank lines. \
No "you", no "create", no "make sure".
- NEVER use quality boosters: no masterpiece, no 8K, no highly detailed, no award-winning, \
no best quality, no professional photography.
- Name colours with a modifier: deep navy, muted olive, pale cream, warm terracotta, blue-grey. \
Never bare "red" or "blue".
- Give the material, not just the noun: brushed metal, frosted glass, coarse linen, weathered wood.
- Enumerate. Never summarise. "Several items" is not a description.
- People get their observable surface: build, posture, gaze, expression, hair, skin tone, and each \
garment with its colour and material. Age is a life stage - a child, a teenager, a young adult, \
middle-aged, in her thirties - never a number of years. If the face is turned away or cropped, \
say that instead of describing it.
- Objects by class, not brand, unless the user named the brand.
- Hedge what is uncertain: "appears to be", "likely", "a notebook or a tablet".
- Everything holds together physically: shadows fall away from the light, reflections match, \
scale is consistent.
"""

SYS_LENGTH = {
    "short": "Length: about 120 words, roughly 6 to 8 sentences.",
    "long": ("Length: about 450 words, roughly 20 sentences at about 25 words each. "
             "A short brief never buys a short description - you invent the rest of the frame."),
}

SYS_LANG = {
    "en": "Language: write the description in ENGLISH only. "
          "Exception: any text that should be READ inside the image stays in its own script, "
          "wrapped in straight double quotes.",
    "zh": "Language: write the description in CHINESE (简体中文). "
          "Any text that should be READ inside the image keeps its original script, "
          "wrapped in straight double quotes.",
}

# 多方向时的附加格式要求
SYS_MULTI = """
Output format: write exactly {n} SELF-CONTAINED descriptions of the SAME scene. Each one must \
be rendered IN a different style direction - not the same style written {n} times, and not one \
long description chopped into {n} pieces.
Each description must stand on its own: name its medium in the opening sentence, then describe \
the WHOLE scene in that style.
Separate two descriptions with a line containing ONLY three hyphens, like this:
---
Write ALL {n} descriptions - never stop after the first one.
No numbering, no labels, no preamble, no markdown. Just the {n} descriptions, in order.
Style directions, in order:
{directions}
"""

# 中文版：实测纯英文的格式说明在 lang=zh 时遵守率明显下降（3 版只回 1 版），
# 所以中文场景用中文再写一遍格式要求。
SYS_MULTI_ZH = """
输出格式：写 **{n} 段互相独立**的描述，都是同一个场景，但**每段必须换一种风格方向来写**
—— 不是同一种风格写 {n} 遍，也不是把一整段描述切成 {n} 块。
每段都要能独立成立：**开场句就点明这段用什么媒介**，然后**把整个场景**用这种风格描述完。
每两段之间**单独占一行**放三个连字符作为分隔，像这样：
---
**{n} 段都要写完整，不要只写第一段就停。**
不要编号、不要小标题、不要前言、不要 markdown。只输出这 {n} 段正文，按顺序排列。
风格方向（严格按顺序：第 1 段用第 1 个方向，第 2 段用第 2 个方向，以此类推）：
{directions}
"""

# 每段「开场是否点明自己的媒介」的判据（官方八步法第 3 步：开场句点明媒介）
_STYLE_MARK = {
    "photo": ("photograph", "photographic", "camera", "lens", "aperture", "documentary",
              "35mm", "摄影", "照片", "实拍", "镜头", "焦段", "光圈", "纪录片"),
    "cinema": ("cinematic", "film still", "anamorphic", "wide screen", "widescreen", "grain",
               "dramatic light", "电影", "胶片", "颗粒", "宽银幕", "宽幅", "影调", "戏剧光"),
    "art": ("illustration", "illustrated", "graphic", "poster", "vector", "silkscreen",
            "flat colour", "flat color", "comic", "插画", "平面", "色块", "海报", "矢量",
            "扁平", "丝网", "漫画"),
}


def _style_ok(out, dirs, head=140):
    """
    每段开场是否点明了自己的媒介。

    ⚠️ 实测中文场景下 PE 会「不换风格」：3 段其实是一段连续描述被切开，
    第 2 段（该是电影感）写着「这是一张**写实摄影**风格的夜景照片」。
    段数和互异性都过了，但用户要的「3 个不同方向」没实现 —— 所以必须单独查这一条。
    """
    for o, d in zip(out, dirs):
        t = (o.get("text") or "")[:head].lower()
        if not any(m.lower() in t for m in _STYLE_MARK.get(d.get("key"), ())):
            return False
    return True


def _multi_user(text, count, dirs, lang, strict=False):
    """多方向时的 user 轮 —— 格式与风格要求必须在这里再写一遍（user 轮约束力更强）。"""
    if lang == "zh":
        seg = "；".join(f"第 {i+1} 段用「{d['label']}」的风格"
                        for i, d in enumerate(dirs))
        s = (f"请输出 {count} 段描述，都是同一个场景，段与段之间单独一行放三个连字符 ---。"
             f"{seg}。每段开头就要点明该段的风格"
             f"（写实摄影要出现摄影/镜头/照片，电影感要出现电影/胶片/颗粒/宽银幕，"
             f"插画平面要出现插画/平面/色块/海报）。"
             f"每段都要把整个场景描述完整，不要接着上一段继续写，也不要只写画面的一角。")
        if strict:
            s += ("这是硬性要求：各段风格必须明显不同、各自独立成篇。"
                  "上一轮你没有做到，请重写。")
    else:
        seg = ", ".join(f"description {i+1} in the {d['label']} direction"
                        for i, d in enumerate(dirs))
        s = (f"Output exactly {count} descriptions of this same scene, separated by a line "
             f"containing exactly three hyphens (---): {seg}. "
             f"Open each description by naming its medium. Each description must cover the "
             f"WHOLE scene in its own style - do not continue the previous one and do not "
             f"zoom into one corner. No numbering, no labels, nothing else.")
        if strict:
            s += (" This is mandatory: the descriptions must differ clearly in style and each "
                  "must stand alone. You did not do that last time - rewrite them.")
    return f"{text}\n\n{s}"


def _http_json(url, payload=None, timeout=180):
    """极简 JSON HTTP 客户端（绕系统代理）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {"User-Agent": "Qwen21Web/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


# --------------------------------------------------------------------------
# 显存探测 —— 决定 PE 这次走卡还是走 CPU
# --------------------------------------------------------------------------
def free_vram_mib():
    """独显当前空闲显存（MiB）。探测不到返回 None（无独显 / 无 nvidia-smi）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:
        return None


def resolve_num_gpu(need=None):
    """
    把 PE_NUM_GPU 的配置值解析成 Ollama 的 num_gpu。

    * "auto" → 空闲显存 >= need 时返回 -1（让 Ollama 全部放卡上），否则 0
    * 其它   → 原样返回整数（0 = 纯 CPU，-1 = 交给 Ollama 判断）
    """
    need = need if need is not None else PE_VRAM_NEED
    if str(PE_NUM_GPU).strip().lower() != "auto":
        try:
            return int(PE_NUM_GPU)
        except (TypeError, ValueError):
            return 0
    free = free_vram_mib()
    return -1 if (free is not None and free >= need) else 0


def resolve_keep_alive(num_gpu):
    """
    按设备决定 keep_alive。上卡就立刻卸（8 GB 卡不能让 PE 长期占着），
    纯 CPU 就常驻（省掉 117.8 秒冷加载）。
    """
    if str(PE_KEEP_ALIVE).strip().lower() != "auto":
        return PE_KEEP_ALIVE
    return 0 if num_gpu else PE_KEEP_ALIVE_CPU


# --------------------------------------------------------------------------
# PE 模型（官方微调）探测
# --------------------------------------------------------------------------
_PE_CACHE = {"at": 0.0, "ok": False}
_PE_TTL = 30.0          # 秒


def pe_available(force=False):
    """
    Ollama 里是否装了官方 PE 模型。

    ⚠️ 缓存**必须带 TTL**，不能一查定终身：出图台常比 Ollama 先启动，
    永久缓存会把启动那一刻的 False 一直记着 —— 表现为网页上一直显示
    「扩写：通用模型」，即使 PE 早就装好了（本机真实踩过）。
    """
    now = time.time()
    if not force and _PE_CACHE["at"] and now - _PE_CACHE["at"] < _PE_TTL:
        return _PE_CACHE["ok"]
    st, obj = _http_json(OLLAMA + "/api/tags", timeout=6)
    ok = False
    if st == 200:
        names = {m.get("name", "") for m in obj.get("models", [])}
        ok = any(n == PE_MODEL or n.startswith(PE_MODEL + ":") for n in names)
    _PE_CACHE.update(at=now, ok=ok)
    return ok


def active_model():
    return PE_MODEL if pe_available() else FALLBACK_MODEL


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def _clean(s):
    s = (s or "").strip()
    for pre in ("Prompt:", "prompt:", "Description:", "Here is", "Sure,", "```"):
        while s.startswith(pre):
            s = s[len(pre):].strip()
    s = s.strip().strip("`").strip()
    if s.startswith('"') and s.endswith('"') and len(s) > 2:
        s = s[1:-1].strip()
    return s


# ---- 官方 system prompt（PE 路线专用）----
_OFFICIAL_SYS = None


def official_sys():
    """读官方 PE-T2I 的 system prompt 原文（八步法）。读不到就退回压缩版。"""
    global _OFFICIAL_SYS
    if _OFFICIAL_SYS is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "prompts", "system_prompt_t2i.txt")
        try:
            with open(p, encoding="utf-8") as f:
                _OFFICIAL_SYS = f.read().strip()
        except Exception:
            _OFFICIAL_SYS = SYS_CORE + "\n" + SYS_LENGTH["long"] + "\n" + SYS_LANG["en"]
    return _OFFICIAL_SYS


def _parse_json_prompt(raw):
    """从 PE 输出里取 (rewritten_prompt, wh_ratio)。官方格式是单行 JSON。"""
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
            txt = (obj.get("rewritten_prompt") or "").strip()
            ratio = (obj.get("wh_ratio") or "").strip()
            if txt:
                return txt, ratio
        except Exception:
            pass
    # JSON 不合法时用正则兜底捞 rewritten_prompt
    m = re.search(r'"rewritten_prompt"\s*:\s*"(.*?)"\s*,\s*"wh_ratio"', s, re.S)
    if m:
        try:
            txt = json.loads('"' + m.group(1) + '"')
        except Exception:
            txt = m.group(1)
        r2 = re.search(r'"wh_ratio"\s*:\s*"([^"]*)"', s)
        return txt.strip(), (r2.group(1).strip() if r2 else "")
    return _clean(s), ""


def _strip_think(raw):
    return re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()


def _split_multi(raw, n):
    """
    按分隔符切分多方向输出，取「切得最碎」的那一种切法。

    ⚠️ 实测：只把格式要求写在 system 里，PE 约 1/3 概率整段不分隔（3 版只回 1 版）。
    所以返回的是「尽力而为」的列表（可能不足 n 个），由调用方判断要不要重试。
    """
    s = (raw or "").strip()
    if not s:
        return []
    best = []
    for rx in (r"\n[ \t]*[-*_]{3,}[ \t]*\n",     # 标准：独占一行
               r"[ \t]*[-*_]{3,}[ \t]*",          # 贴在行首/行尾
               r"\n\s*\n"):                       # 退化成空行分段
        parts = [p.strip() for p in re.split(rx, s) if len(p.strip()) > 40]
        if len(parts) > len(best):
            best = parts
        if len(best) >= n:
            break
    return best[:n] if best else [s]


def _probe_re(p):
    """把方向名/提示词变成宽松正则：空白可以任意（'插画 / 平面' 也匹配 '插画/平面'）。"""
    return re.escape(p).replace(r"\ ", r"\s*")


def _dir_hit(line, dirs):
    """这一行是否命中了某个方向名或它的提示词。"""
    low = line.lower()
    for d in dirs or []:
        for probe in (d.get("label", ""), d.get("hint", "").split(",")[0]):
            p = (probe or "").strip()
            if p and p.lower() in low:
                return True
    return False


def _strip_dir_echo(text, dirs):
    """
    掐掉正文开头被原样抄进来的「方向清单」。

    ⚠️ 实测：PE 有概率把 system 里那段 `1. 写实摄影: a realistic photograph…`
    当成正文开头照抄，导致第 2/3 段带着上一段的方向标题 —— 提示词直接拿去出图
    会把「a realistic photograph」当成画面内容描述。

    两步：① 整行都是方向清单的，删掉整行；② 剩下若以「方向名 + 冒号」开头，只掐前缀。
    """
    s = (text or "").strip()
    if not s or not dirs:
        return s

    # ① 删掉开头的方向清单整行
    lines = s.split("\n")
    while len(lines) > 1:
        head = lines[0].strip()
        if not head:
            lines.pop(0)
            continue
        if _dir_hit(head, dirs) and len(head) < 180:
            lines.pop(0)
            continue
        break
    s = "\n".join(lines).strip()

    # ② 单行的情况：只掐「方向名 + 冒号/逗号」这个前缀，别把正文一起删了
    for d in dirs:
        for probe in (d.get("label", ""), d.get("hint", "").split(",")[0]):
            p = (probe or "").strip()
            if not p:
                continue
            m = re.match(r"^\s*\d{0,2}\s*[.、)]?\s*" + _probe_re(p) + r"\s*[:：,，]\s*",
                         s, re.I)
            if m:
                return s[m.end():].strip()
    return s


def _ollama_chat(model, system, user, options, timeout, think=False,
                 keep_alive=None):
    """
    走 /api/chat —— 思考内容会落在 message.thinking，不会混进 message.content。
    /api/generate 在思考模型上会把 CoT 和答案糊在一起，实测踩过。
    """
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "keep_alive": PE_KEEP_ALIVE if keep_alive is None else keep_alive,
        "think": think,
        "options": options,
    }
    st, obj = _http_json(OLLAMA + "/api/chat", payload, timeout=timeout)
    if st != 200 and "think" in str(obj.get("error", "")).lower():
        # 老版本 Ollama 不认 think 字段 —— 去掉重试
        payload.pop("think", None)
        st, obj = _http_json(OLLAMA + "/api/chat", payload, timeout=timeout)
    if st != 200:
        raise RuntimeError(obj.get("error") or f"HTTP {st}")
    msg = obj.get("message") or {}
    txt = (msg.get("content") or "").strip()
    if not txt:                       # think=true 时答案可能只落在 thinking 里
        txt = (msg.get("thinking") or "").strip()
    txt = _strip_think(txt)
    if not txt:
        raise RuntimeError("模型返回空内容")
    return txt, obj


# --------------------------------------------------------------------------
# 路线 A —— 官方 PE 微调模型（推荐）
# --------------------------------------------------------------------------
def _enhance_pe(text, count, lang, timeout):
    """
    官方 PE 微调模型。

    两条子路线：
    * 压缩 prompt（默认，CPU 友好）：一次调用出全部方向，按 --- 切分。
      CPU 上 4 tok/s，分开跑 3 次要 5 分钟以上，合并成一次省 2/3 时间。
    * 官方长 prompt（PE_FULL_PROMPT=1，GPU 友好）：官方要单行 JSON
      {"rewritten_prompt":..., "wh_ratio":...}，一次只能问一个方向，逐个跑。
    """
    model = PE_MODEL
    full = PE_FULL_PROMPT
    dirs = DIRECTIONS[:count] if count > 1 else []
    if full:
        system = official_sys() + "\n" + SYS_LANG[lang]
        ngpu = resolve_num_gpu()
        ka = resolve_keep_alive(ngpu)
        opts = {
            "num_gpu": ngpu,
            "temperature": 1.0, "top_p": 0.95, "top_k": 20,
            "presence_penalty": 1.5, "num_predict": 4096,
        }
        out, ratios = [], []
        for d in (dirs or [None]):
            user = text if d is None else (
                f"{text}\n\nRender it as {d['hint']}. "
                f"Keep every element the user named.")
            raw, _ = _ollama_chat(model, system, user, opts, timeout, think=False,
                                  keep_alive=ka)
            txt, ratio = _parse_json_prompt(raw)
            if not txt:
                continue
            out.append({"direction": d["key"] if d else "single",
                        "label": d["label"] if d else "单版本", "text": txt})
            if ratio:
                ratios.append(ratio)
        if not out:
            raise RuntimeError("PE 返回空内容")
        meta = {"source": "pe", "model": model}
        if ratios:
            meta["wh_ratio"] = ratios[0]
        return out, meta

    # ---- 压缩 prompt 路线：一次调用出全部方向 ----
    parts = [SYS_CORE, SYS_LENGTH["short"], SYS_LANG[lang]]
    if count > 1:
        # 中文场景用中文再写一遍格式要求：实测纯英文格式说明在 lang=zh 时遵守率更低
        tpl = SYS_MULTI_ZH if lang == "zh" else SYS_MULTI
        parts.append(tpl.format(
            n=count,
            directions="\n".join(f"{i+1}. {d['label']}: {d['hint']}"
                                 for i, d in enumerate(dirs))))
    ngpu = resolve_num_gpu()
    ka = resolve_keep_alive(ngpu)
    opts = {
        "num_gpu": ngpu,
        "temperature": 0.9 if count > 1 else 0.85,
        "top_p": 0.95, "top_k": 20,
        "presence_penalty": 1.5,
        # 每段 ~170 token，给 2.5 倍余量，免得第 3 段被截断
        "num_predict": 420 * max(1, count),
    }
    system = "\n".join(parts)
    # ⚠️ 格式与风格要求必须同时写进 user 轮：实测只放 system 里，PE 约 1/3 概率整段不分隔
    # （3 版只回 1 版），而且中文场景还会「不换风格」（3 段其实是一段连续描述被切开）。
    # user 轮对这类要求的约束力明显更强。

    def _once(o, strict=False):
        u = _multi_user(text, count, dirs, lang, strict) if count > 1 else text
        r, _ = _ollama_chat(model, system, u, o, timeout,
                            think=False, keep_alive=ka)
        if count == 1:
            return [{"direction": "single", "label": "单版本",
                     "text": _clean(_strip_dir_echo(r, []))}]
        ts = _split_multi(r, count)
        return [{"direction": d["key"], "label": d["label"],
                 "text": _clean(_strip_dir_echo(t, dirs))}
                for d, t in zip(dirs, ts)]

    def _score(res):
        """(段数, 是否每段都换了风格) —— 两个都要满足才算合格。"""
        got = len([o for o in res if o["text"]])
        return (got, 1 if (got == count and _style_ok(res, dirs)) else 0)

    out = _once(opts)
    if count > 1 and _score(out) < (count, 1):
        # 模型没听话（段数不够 / 没换风格）—— 降温度 + 加硬话重试一次，取更好的那次
        retry = _once(dict(opts, temperature=0.6, presence_penalty=0.8), strict=True)
        if _score(retry) > _score(out):
            out = retry
    out = [o for o in out if o["text"]]
    if not out:
        raise RuntimeError("PE 返回空内容")
    meta = {"source": "pe", "model": model}
    if count > 1:
        if len(out) < count:
            # 实测约 1/3 概率模型不遵守 --- 分隔（中文更高），重试也可能无效
            meta["note"] = (f"模型只给出 {len(out)}/{count} 段（分隔符未遵守，已自动重试一次）"
                            f"，再点一次扩写通常能拿到完整 {count} 段")
        elif not _style_ok(out, dirs):
            meta["note"] = (f"{count} 段都拿到了，但有段落没明显切换风格"
                            f"（已自动重试一次）；再点一次扩写通常更好")
    return out, meta


# --------------------------------------------------------------------------
# 路线 B —— 通用模型（同一个压缩版 prompt，兜底 / 本机默认）
# --------------------------------------------------------------------------
def _enhance_generic(text, count, lang, length, timeout):
    model = FALLBACK_MODEL
    if length is None:
        length = "short"
    parts = [SYS_CORE, SYS_LENGTH[length], SYS_LANG[lang]]
    dirs = DIRECTIONS[:count] if count > 1 else []
    if count > 1:
        # 中文场景用中文再写一遍格式要求：实测纯英文格式说明在 lang=zh 时遵守率更低
        tpl = SYS_MULTI_ZH if lang == "zh" else SYS_MULTI
        parts.append(tpl.format(
            n=count,
            directions="\n".join(f"{i+1}. {d['label']}: {d['hint']}"
                                 for i, d in enumerate(dirs))))
    system = "\n".join(parts)
    opts = {
        "num_gpu": FB_NUM_GPU,
        "temperature": 0.85 if count == 1 else 0.9,
        # 3 方向要一次吐 3 段，token 预算得给够，否则第 3 段被截断
        "num_predict": (300 if length == "short" else 900) * max(1, count),
        "top_p": 0.9,
        "repeat_penalty": 1.15,
    }

    def _once(strict=False):
        u = (_multi_user(text, count, dirs, lang, strict) if count > 1
             else f"Idea: {text}\n\nDescription:")
        r, _ = _ollama_chat(model, system, u, opts, timeout, think=False)
        if count == 1:
            return [{"direction": "single", "label": "单版本",
                     "text": _clean(_strip_dir_echo(r, []))}]
        ts = _split_multi(r, count)
        return [{"direction": d["key"], "label": d["label"],
                 "text": _clean(_strip_dir_echo(t, dirs))}
                for d, t in zip(dirs, ts)]

    def _score(res):
        got = len([o for o in res if o["text"]])
        return (got, 1 if (got == count and _style_ok(res, dirs)) else 0)

    out = _once()
    if count > 1 and _score(out) < (count, 1):
        retry = _once(strict=True)          # 兜底路线模型更弱，一样给一次重试
        if _score(retry) > _score(out):
            out = retry
    out = [o for o in out if o["text"]]
    if not out:
        raise RuntimeError("模型返回空内容")
    meta = {"source": "ollama", "model": model}
    if count > 1 and len(out) < count:
        meta["note"] = (f"模型只给出 {len(out)}/{count} 段（分隔符未遵守，已自动重试一次）"
                        f"，再点一次扩写通常能拿到完整 {count} 段")
    return out, meta


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def enhance(text, mode="t2i", count=1, lang="en", length=None, timeout=None):
    """
    把一句想法扩写成图像描述。

    count=1  → [{"direction": "single", "label": "单版本", "text": ...}]
    count=3  → 三个方向的版本（PE 路线跑 3 次，通用路线一次生成 3 段）

    返回 (list_of_dicts, meta)；meta = {"source": "pe"/"ollama", "model": ...}
    失败抛 RuntimeError，由调用方决定是否走离线兜底。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("提示词为空")
    count = max(1, min(3, int(count or 1)))
    lang = lang if lang in SYS_LANG else "en"
    timeout = timeout or PE_TIMEOUT

    if mode == "bgremove":
        return ([{"direction": "single", "label": "固定指令",
                  "text": "Remove the background, and output a PNG image"}],
                {"source": "fixed", "model": "-"})

    if pe_available():
        return _enhance_pe(text, count, lang, timeout)
    return _enhance_generic(text, count, lang, length, timeout)


# --------------------------------------------------------------------------
# 离线兜底 —— 按官方思路「补结构」，不堆质量词
# --------------------------------------------------------------------------
_OFFLINE_FALLBACK = {
    "photo": "shot on 85mm f/1.4 with shallow depth of field",
    "cinema": "anamorphic framing, teal-and-orange grade, subtle film grain",
    "art": "flat vector shapes, strong graphic composition",
}
_OFFLINE_LIGHT = [
    "The lighting is soft diffused daylight from a window on the left.",
    "The lighting is warm low golden-hour sun raking across the scene.",
    "The lighting is hard overhead studio light, leaving crisp shadows below.",
    "The lighting is flat even ambient light, with no strong shadow.",
]
_OFFLINE_CLOSE = [
    "The overall composition balances a centred subject against open space, "
    "in a muted palette, and reads as calm and contemplative.",
    "The overall composition uses strong diagonal tension, in a warm palette, "
    "and reads as energetic and bold.",
]


def offline_enhance(text, mode="t2i", count=1):
    """Ollama 完全不可用时的兜底：补结构（光线句 + 收尾句），不堆质量词。"""
    if mode == "bgremove":
        return [{"direction": "single", "label": "固定指令",
                 "text": "Remove the background, and output a PNG image"}]
    import random as _r
    base = text.strip()
    rnd = _r.Random(base)
    dirs = DIRECTIONS[:count] if count > 1 else [{"key": "photo", "label": "单版本",
                                                  "hint": _OFFLINE_FALLBACK["photo"]}]
    out = []
    for d in dirs:
        hint = d.get("hint") or _OFFLINE_FALLBACK.get(d["key"], "")
        body = f"{base}, {hint}. {rnd.choice(_OFFLINE_LIGHT)} {rnd.choice(_OFFLINE_CLOSE)}"
        out.append({"direction": d["key"], "label": d["label"], "text": body})
    return out


def lexicon_payload():
    """给前端的词库 JSON。"""
    return {
        "groups": [{"key": k, "label": v["label"], "items": v["items"]}
                   for k, v in LEXICON.items()],
        "directions": [{"key": d["key"], "label": d["label"]} for d in DIRECTIONS],
        "presets": [{"key": p["key"], "label": p["label"], "desc": p["desc"],
                     "text": p["text"], "negative": p.get("negative", "")}
                    for p in STYLE_PRESETS],
        "pe_available": pe_available(force=True),
        "pe_model": PE_MODEL,
        "fallback_model": FALLBACK_MODEL,
    }


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    print("Ollama:", OLLAMA)
    print("PE 可用:", pe_available(force=True), "| 当前模型:", active_model())
    print("\n词库分组:")
    for g in lexicon_payload()["groups"]:
        print(f"  {g['label']:<10} {len(g['items'])} 项")
    if len(sys.argv) > 1:
        idea = sys.argv[1]
        cnt = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        print(f"\n扩写: {idea!r}  (count={cnt})")
        try:
            res, meta = enhance(idea, count=cnt)
            print("meta:", meta)
            for r in res:
                print(f"\n--- [{r['label']}] ---\n{r['text']}")
        except Exception as e:
            print("失败:", e)
            for r in offline_enhance(idea, count=cnt):
                print(f"\n--- [offline {r['label']}] ---\n{r['text']}")
