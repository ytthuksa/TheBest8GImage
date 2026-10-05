# -*- coding: utf-8 -*-
"""
Qwen-Image-2.1 出图环境 —— 「一键安装包」打包器（2026-10-02 新建）

跟旧的 _pack_for_migration.py 的区别：
    旧版 = 「迁移目录」，新机要自己 git clone ComfyUI、自己装 Python、自己建 venv。
    本版 = 「安装包」，把上游源码 + 便携 Python 一起带上，新机解压后双击 安装.bat 就完事。

用法（在 C:\\ComfyUI 下跑）：
    .venv\\Scripts\\python.exe _make_installer.py --dest X:\\Qwen21_安装包
    ... --dry-run          # 只列清单、算体积，不落盘
    ... --utf8             # 强制 UTF-8 输出（给 AI/管道用；双击默认 GBK）
    ... --no-pe            # 不带提示词扩写模型（省 5.24 GB，扩写功能不可用）
    ... --no-r64           # 不带旧版 4 步加速 LoRA（省 0.32 GB）
    ... --with-styletest   # 带上风格验证图（+110 MB，一般不需要）

产物结构：
    <dest>\\
    ├─ 安装.bat                <- 新机双击这个
    ├─ 使用说明-新机.md
    ├─ _reqs_notorch.txt       <- 依赖清单（已剔除 torch，torch 单独装 cu126 版）
    ├─ _install_check.py       <- 安装自检脚本
    ├─ python\\                <- 便携 CPython 3.13.12（73 MB）
    └─ ComfyUI\\               <- 上游源码 + 自研代码 + 模型 + 自定义节点

目标机前提（用户 2026-10-02 确认）：能联网 + NVIDIA 独显 ≥ 8 GB。
"""
import argparse
import os
import shutil
import sys
import time

SRC = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 便携 Python
PORTABLE_PY = r"C:\PortablePython"

# ---------------------------------------------------------------- 排除规则
# 整棵目录树里这些目录名直接跳过（任何层级）
SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", "_chromeprofile", ".pytest_cache",
                  ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
                  # Fisher 插件的「已存姿势」是用户运行时数据（user/default/fisher_pose/poses/），
                  # 不是程序文件，不该跟着安装包发给别人
                  "fisher_pose"}

# 顶层目录整个跳过（各自有别的处理方式，或纯属本地产物）
SKIP_TOP_DIRS = {"output", "input", "temp", "models", "_wx", "_installer_preview"}

# 文件级排除：这些是本地实验残留 / 缓存 / 本地状态，不该进安装包
SKIP_FILE_PREFIX = ("_bak", "_cmp_", "_probe_L", "_igpu_test")
SKIP_FILE_NAMES = {
    "_object_info.json", "_te_tree.json", "_web_jobs.json", "_lan_url.txt",
    "_last_run.json", "_ab_resp.json", "_cleanup_check.txt", "_ui.png",
    "_defaults.png", "_defaults_tall.png", "_web_server.log", "dl6.log",
    "_download.log", "_download_te.log", "_pe_download.log",
    "_igpu_test2_ollama.log", "_igpu_test_ollama.log", "_igpu_test_resp.json",
    # 本机调试残留：临时输出 / 自驱动测试页（_web 下，但只是开发脚手架）
    "_cleanup_try.txt", "_posetest.html",
}
# .db-shm / .db-wal / .db-journal 是 SQLite 的运行时边车文件，拷过去只会带来不一致
SKIP_FILE_SUFFIX = (".pyc", ".log", ".bak", ".tmp", ".part",
                    ".db-shm", ".db-wal", ".db-journal")

# ---------------------------------------------------------------- 模型清单
# (相对 ComfyUI 的路径, 人话说明, 开关标签 None=必带)
MODELS_REQUIRED = [
    ("models/diffusion_models/qwen-image-2.1-Q4_K_M.gguf", "DiT 主模型 (GGUF Q4_K_M)", None),
    ("models/text_encoders/qwen3vl_8b_w4a8.safetensors", "文本编码器 Qwen3-VL-8B (w4a8)", None),
    ("models/vae/qwen_image_2.1_vae_bf16.safetensors", "VAE", None),
    ("models/loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors",
     "加速 LoRA v0.2.1 r128（前端默认挂的就是它）", None),
    ("models/loras/VNCCS_QI2_PoseStudioV1.1.safetensors",
     "姿势编辑 LoRA（「姿势编辑」档必需，强度固定 1.0）", None),
]
MODELS_OPTIONAL = [
    ("models/prompt_enhancer/Qwen-Image-2.1-PE-T2I.Q4_K_M.gguf", "提示词扩写模型 PE-T2I", "pe"),
    ("models/prompt_enhancer/Modelfile", "Ollama 注册 PE 用的 Modelfile", "pe"),
    ("models/loras/Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors",
     "旧版加速 LoRA v0.1 r64（只有 4 步档）", "r64"),
]
# 整个目录带走的模型附属（体积都很小）
MODELS_DIRS = [
    ("models/configs", "模型配置 (LoRA 元数据等)"),
]

# ---------------------------------------------------------------- 模板文件
INSTALL_BAT = r"""@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title Qwen-Image-2.1 出图环境 - 一键安装
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYDIR=%ROOT%\python"
set "PY=%PYDIR%\python.exe"
set "CFY=%ROOT%\ComfyUI"
set "VENV=%CFY%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "TMPGPU=%TEMP%\qwen21_gpu.txt"

echo ============================================================
echo    Qwen-Image-2.1 出图环境 - 一键安装
echo ============================================================
echo    安装目录 : %ROOT%
echo    目标位置 : %CFY%
echo.
echo    整个过程约 10~25 分钟（大头是下载 PyTorch cu126 约 2.5 GB）。
echo    中途可以 Ctrl+C 中断，重新双击会接着装（已完成的步骤自动跳过）。
echo.
pause

rem ---------------------------------------------------------- [1/8] 检查包完整性
echo.
echo [1/8] 检查安装包完整性...
if not exist "%PY%" (
  echo    [失败] 找不到便携 Python: %PY%
  echo           安装包不完整，请重新解压。
  goto :fail
)
if not exist "%CFY%\main.py" (
  echo    [失败] 找不到 %CFY%\main.py
  echo           安装包不完整，请重新解压。
  goto :fail
)
echo    [OK] 便携 Python 与 ComfyUI 源码都在。
echo.

rem ---------------------------------------------------------- [2/8] 检查显卡
echo [2/8] 检查显卡与驱动...
where nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo    [失败] 找不到 nvidia-smi，说明本机没装 NVIDIA 驱动。
  echo           这套环境只支持 NVIDIA 显卡（CUDA），AMD / Intel 核显跑不了。
  echo           先装驱动再回来: https://www.nvidia.cn/drivers/
  goto :fail
)
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader > "%TMPGPU%" 2>nul
set /p GPUINFO=<"%TMPGPU%"
echo    显卡: !GPUINFO!
nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits > "%TMPGPU%" 2>nul
for /f "tokens=1 delims= " %%a in ('type "%TMPGPU%"') do set "VRAM=%%a"
if not defined VRAM set "VRAM=0"
if !VRAM! LSS 8000 (
  echo    [警告] 显存只有 !VRAM! MiB，低于 8 GB。
  echo           实测峰值占用 7.45 GiB，低于 8 GB 很可能 OOM。
  echo           仍然继续，但出图可能失败。
) else (
  echo    [OK] 显存 !VRAM! MiB，够用。
)
echo.

rem ---------------------------------------------------------- [3/8] 建虚拟环境
echo [3/8] 创建 Python 虚拟环境...
if exist "%VPY%" (
  echo    [OK] 已存在，跳过（想重建就先删掉 ComfyUI\.venv）。
  goto :step4
)
"%PY%" -m venv "%VENV%"
if errorlevel 1 (
  echo    [失败] 建 venv 失败。看看上面报了什么。
  goto :fail
)
echo    [OK] 已创建 %VENV%
:step4
echo.

rem ---------------------------------------------------------- [4/8] 升级 pip
echo [4/8] 升级 pip...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
echo    [OK]
echo.

rem ---------------------------------------------------------- [5/8] 装 torch
echo [5/8] 安装 PyTorch（CUDA 12.6 专用源）...
echo        约 2.5 GB，慢的话十分钟以上，别关窗口。
"%VPY%" -c "import torch" >nul 2>nul
if not errorlevel 1 (
  echo    [OK] torch 已装，跳过。
  goto :step6
)
"%VPY%" -m pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cu126
if errorlevel 1 (
  echo    [失败] torch 装不上。
  echo           检查能不能访问 https://download.pytorch.org/whl/cu126
  echo           公司网如果拦了，配一下代理再重跑。
  goto :fail
)
:step6
echo.

rem ---------------------------------------------------------- [6/8] 装其余依赖
echo [6/8] 安装其余依赖（transformers / gguf / pillow 等）...
"%VPY%" -m pip install -r "%ROOT%\_reqs_notorch.txt"
if errorlevel 1 (
  echo    [失败] 依赖装不上。
  goto :fail
)
echo    [OK]
echo.

rem ---------------------------------------------------------- [7/8] 自检
echo [7/8] 环境自检...
"%VPY%" "%ROOT%\_install_check.py"
if errorlevel 1 (
  echo.
  echo    [失败] 自检没过，看上面的具体哪一项报红。
  goto :fail
)
echo.

rem ---------------------------------------------------------- [8/8] Ollama + 快捷方式
echo [8/8] 注册 Ollama 模型 + 建桌面快捷方式...
where ollama >nul 2>nul
if errorlevel 1 (
  echo    [跳过] 没装 Ollama。提示词「扩写 / 维度」功能会退化成离线规则版（质量差很多）。
  echo           要完整功能就装一下: https://ollama.com/download
  echo           装完再双击本脚本，这一步会自动补上。
) else (
  if exist "%CFY%\models\prompt_enhancer\Modelfile" (
    pushd "%CFY%\models\prompt_enhancer"
    ollama create qwen-image-pe-t2i -f Modelfile
    popd
    echo    [OK] 已注册扩写模型 qwen-image-pe-t2i
  ) else (
    echo    [跳过] 包里没带 PE 模型（打包时用了 --no-pe）。
  )
  ollama list | findstr /i "qwen2.5-abliterate" >nul
  if errorlevel 1 (
    echo    [提示] 没有离线兜底模型，建议补一个:
    echo           ollama pull huihui_ai/qwen2.5-abliterate:7b-instruct
  ) else (
    echo    [OK] 离线兜底模型已就位。
  )
)
set "LNK=%USERPROFILE%\Desktop\Qwen21 出图台.lnk"
set "SC_TARGET=%ROOT%\启动出图台.bat"
set "SC_WORKDIR=%ROOT%"
if exist "%ROOT%\启动出图台.bat" (
  powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($d+'\Qwen21 出图台.lnk'); $s.TargetPath=$env:SC_TARGET; $s.WorkingDirectory=$env:SC_WORKDIR; $s.Description='Qwen-Image-2.1 出图台'; $s.Save()" 2>nul
  if exist "%LNK%" (
    echo    [OK] 桌面快捷方式已创建: Qwen21 出图台
  ) else (
    echo    [跳过] 快捷方式没建成（不影响使用，直接双击 启动出图台.bat 就行）。
  )
) else (
  echo    [跳过] 包里没有 启动出图台.bat。
)
echo.

echo ============================================================
echo    安装完成
echo ============================================================
echo.
echo    下一步：
echo      1. 双击 %ROOT%\启动出图台.bat
echo      2. 浏览器自动打开 http://127.0.0.1:8190
echo      3. 第一次出图要 1~3 分钟加载模型，之后就快了
echo.
echo    想让手机也能出图：右键 放行出图台端口.bat -^> 以管理员身份运行（只需一次）
echo.
echo    详细说明看 %ROOT%\使用说明-新机.md
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo    安装中断
echo ============================================================
echo    修掉上面报的问题，再双击 安装.bat 重跑即可（已完成步骤会跳过）。
echo.
pause
exit /b 1
"""

INSTALL_CHECK_PY = r'''# -*- coding: utf-8 -*-
"""安装自检 —— 由 安装.bat 第 [7/8] 步调用。退出码 0 = 全过。"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
# 默认查同级 ComfyUI\；也可以显式传一个路径进来（便于在别处验证脚本本身）
CFY = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "ComfyUI")
fails = []


def line(ok, label, detail=""):
    tag = "[OK]  " if ok else "[失败]"
    print("    %s %s%s" % (tag, label, ("  -- " + detail) if detail else ""))
    if not ok:
        fails.append(label)


print("    安装目录: %s" % ROOT)
print()

# 1) torch + CUDA
try:
    import torch
    line(True, "PyTorch 导入", torch.__version__)
    ok = torch.cuda.is_available()
    line(ok, "CUDA 可用", torch.cuda.get_device_name(0) if ok else
         "False —— 装成 CPU 版了？必须用 cu126 源重装 torch")
except Exception as e:
    line(False, "PyTorch 导入", str(e))

# 2) 关键第三方库
for mod, name in [("transformers", "transformers"), ("gguf", "gguf"),
                  ("safetensors", "safetensors"), ("PIL", "Pillow"),
                  ("einops", "einops"), ("numpy", "numpy")]:
    try:
        m = __import__(mod)
        line(True, "依赖 %s" % name, getattr(m, "__version__", ""))
    except Exception as e:
        line(False, "依赖 %s" % name, str(e))

print()

# 3) 模型文件
MODELS = [
    ("models/diffusion_models/qwen-image-2.1-Q4_K_M.gguf", True),
    ("models/text_encoders/qwen3vl_8b_w4a8.safetensors", True),
    ("models/vae/qwen_image_2.1_vae_bf16.safetensors", True),
    ("models/loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors", True),
    ("models/loras/VNCCS_QI2_PoseStudioV1.1.safetensors", True),
    ("models/loras/Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors", False),
    ("models/prompt_enhancer/Qwen-Image-2.1-PE-T2I.Q4_K_M.gguf", False),
]
for rel, required in MODELS:
    p = os.path.join(CFY, rel.replace("/", os.sep))
    if os.path.isfile(p):
        line(True, "模型 %s" % os.path.basename(rel), "%.2f GB" % (os.path.getsize(p) / 2**30))
    else:
        line(not required, "模型 %s" % os.path.basename(rel),
             "缺失" + ("" if required else "（可选，不影响出图）"))

print()

# 4) 自定义节点
for node in ["ComfyUI-GGUF", "ComfyUI-Viggle-Turbo", "ComfyUI-Fisher-Pose"]:
    p = os.path.join(CFY, "custom_nodes", node)
    line(os.path.isdir(p), "自定义节点 %s" % node, "" if os.path.isdir(p) else "缺失")

print()

# 4.5) 姿势编辑器资源（Fisher Pose）
def _human(n):
    for unit, div in (("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


FISHER = os.path.join(CFY, "custom_nodes", "ComfyUI-Fisher-Pose")
for rel, desc in [
    ("web/editor/freepose.html", "姿势编辑器页面"),
    ("web/editor/freepose.mjs", "姿势编辑器逻辑"),
    ("web/vnccs/vnccs_pose_studio_core.mjs", "3D 人偶内核"),
    ("web/vnccs/assets/pose_studio_makehuman.v2.bin", "人偶模型数据"),
    ("FISHER小彩蛋", "内置骨架库"),
]:
    p = os.path.join(FISHER, rel.replace("/", os.sep))
    if os.path.isdir(p):
        line(True, "姿势资源 %s" % desc, "%d 项" % len(os.listdir(p)))
    elif os.path.isfile(p):
        line(True, "姿势资源 %s" % desc, _human(os.path.getsize(p)))
    else:
        line(False, "姿势资源 %s" % desc, "缺失")

print()

# 5) UI 工作流
wf_dir = os.path.join(CFY, "user", "default", "workflows")
for wf in ["qwen_image_2_1_local_t2i.json", "qwen_image_2_1_local_edit.json",
           "qwen_image_2_1_local_bgremove.json", "qwen_image_2_1_local_pose.json"]:
    p = os.path.join(wf_dir, wf)
    line(os.path.isfile(p), "工作流 %s" % wf, "" if os.path.isfile(p) else "缺失")

# 6) 自研代码
for f in ["_server.py", "qwen.py", "_prompt_eng.py", "_free_vram.py",
          "_api_pose.json", "_web/index.html"]:
    p = os.path.join(CFY, f.replace("/", os.sep))
    line(os.path.isfile(p), "程序文件 %s" % f, "" if os.path.isfile(p) else "缺失")

print()
if fails:
    print("    ==> 自检没过 %d 项：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("    ==> 全部通过，可以出图了。")
sys.exit(0)
'''

NEW_MACHINE_DOC = r"""# Qwen-Image-2.1 出图环境 —— 新电脑安装说明

> 打包自：本地开发机（RTX 2000 Ada 8 GB）· 2026-10-02
> 目标机要求：**Windows + NVIDIA 独显 ≥ 8 GB + 能联网**

---

## 0. 三步搞定

```
1. 把整个安装包文件夹拷到新电脑（路径随便，别放中文太深的目录也行）
2. 双击  安装.bat        <- 会自动装 Python 环境、注册 Ollama 模型、建桌面快捷方式
3. 双击  启动出图台.bat   <- 浏览器自动开 http://127.0.0.1:8190
```

**就这样。** 不用装 Python，不用 git clone，不用手动摆模型 —— 全在包里。

---

## 1. 安装前要准备什么

| 项目 | 要求 | 怎么弄 |
|---|---|---|
| 显卡 | **NVIDIA，显存 ≥ 8 GB** | AMD / Intel 核显**跑不了**（整套配方是 CUDA + GGUF） |
| 驱动 | NVIDIA 驱动，支持 CUDA 12.6 | https://www.nvidia.cn/drivers/ |
| 网络 | **能访问 pytorch.org 和 pypi.org** | 装依赖要下载约 3 GB |
| 磁盘 | **≥ 40 GB 空闲** | 包本体 + 新装的 venv 约 5 GB |
| Ollama | 可选但强烈建议 | https://ollama.com/download —— 不装的话提示词「扩写」会退化成离线规则版 |
| 内存 | ≥ 32 GB 比较舒服 | 模型加载要占 |

---

## 2. 安装.bat 都干了什么

八个步骤，**每一步都可以中断重跑**（已完成的会自动跳过）：

| 步骤 | 做什么 |
|---|---|
| `[1/8]` | 检查包完整性（便携 Python + ComfyUI 源码） |
| `[2/8]` | 查显卡与驱动，显存 < 8 GB 会警告 |
| `[3/8]` | 用包里的便携 Python 建 `ComfyUI\.venv` |
| `[4/8]` | 升级 pip |
| `[5/8]` | 装 **PyTorch cu126**（约 2.5 GB，最慢的一步） |
| `[6/8]` | 装其余依赖（transformers / gguf / pillow…） |
| `[7/8]` | 自检：torch+CUDA、依赖、模型、节点、工作流、程序文件 |
| `[8/8]` | 注册 Ollama 扩写模型 + 建桌面快捷方式 |

> ⚠️ **中途失败不要慌** —— 修掉问题再双击一次，前面成功的步骤会跳过，不会重来。

---

## 3. 装完之后

### 3.1 日常出图

双击桌面上的 **「Qwen21 出图台」**（或包里的 `启动出图台.bat`）。
浏览器自动打开 `http://127.0.0.1:8190`，四个 Tab：文生图 / 参考图编辑 / 背景移除 / **姿势编辑**。

> **姿势编辑**（2026-10-04 新增）：先选一张**全身正面**人物照，点「🧊 打开姿势编辑器」，
> 在里面摆 3D 人偶的姿势（左边有 209 张内置骨架可以一键套用），点「应用」再出图。
> 提示词里**别写姿势**（写服装/场景/光线就行）。这一档**不支持加速档**，耗时约 200 秒。
> 详细说明见包里的 `使用说明.md` 第二节。

### 3.2 手机 / 平板也能出图

1. 右键 `放行出图台端口.bat` → **以管理员身份运行**（只需一次）
2. 手机连同一个 WiFi，打开控制台打印的地址（或网页右上角那个胶囊，点一下复制）

> ⚠️ 局域网模式**没有密码**，公共 WiFi 下别这么开。
> 只想本机用：双击 `启动出图台-仅本机.bat`。

### 3.3 出图卡住 / 特别慢

**先双击 `清理显存.bat`。**

8 GB 卡上最常见的坑就是 Ollama 把模型常驻在显存里，导致 ComfyUI 装不下模型、
每步在 CPU↔GPU 之间换页（单步从 10 秒变成 200 秒）。

识别方法：`nvidia-smi` 里 **GPU 占用 99% 但功耗只有 15 W**（正常满负荷 35~44 W）。

`启动出图台.bat` / `启动ComfyUI.bat` 现在**启动时会自动清一次**，所以正常流程不会遇到。

---

## 4. 这个包里有什么

```
安装包\
├─ 安装.bat                 <- 双击这个
├─ 使用说明-新机.md          <- 本文件
├─ _reqs_notorch.txt        <- 依赖清单（torch 单独装 cu126 版，所以剔掉了）
├─ _install_check.py        <- 自检脚本
├─ python\                  <- 便携 CPython 3.13.12（73 MB，不用另装 Python）
└─ ComfyUI\                 <- 整个环境
   ├─ main.py 等            <- ComfyUI 上游源码 0.37.0
   ├─ _server.py            <- 出图台后端（纯标准库）
   ├─ qwen.py               <- 命令行出图入口
   ├─ _prompt_eng.py        <- 提示词扩写引擎
   ├─ _free_vram.py         <- 显存清理
   ├─ _web\index.html       <- 出图台前端（单文件）
   ├─ models\               <- 模型
   ├─ custom_nodes\         <- ComfyUI-GGUF + ComfyUI-Viggle-Turbo
   ├─ user\default\workflows\  <- 3 个 UI 工作流
   └─ *.bat                 <- 启动 / 停止 / 放行端口 / 清理显存
```

### 模型清单

| 文件 | 体积 | 作用 |
|---|---|---|
| `models\diffusion_models\qwen-image-2.1-Q4_K_M.gguf` | 3.91 GB | DiT 主模型 |
| `models\text_encoders\qwen3vl_8b_w4a8.safetensors` | 5.88 GB | 文本编码器 |
| `models\vae\qwen_image_2.1_vae_bf16.safetensors` | 0.63 GB | VAE |
| `models\loras\...viggle-turbo-v0.2.1-6step-lora-r128.safetensors` | 0.63 GB | 加速 LoRA（默认挂载，6 步出图） |
| `models\loras\...viggle-turbo-4step-lora-r64.safetensors` | 0.32 GB | 旧版加速 LoRA（可选） |
| `models\prompt_enhancer\Qwen-Image-2.1-PE-T2I.Q4_K_M.gguf` | 5.24 GB | 提示词扩写（走 Ollama） |

> 📌 打包时**没带** `models\text_encoders\Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf`（4.8 GB）——
> 那是早期实验留下的，生产工作流用的全是 `qwen3vl_8b_w4a8.safetensors`。

---

## 5. 性能参考（原机器，RTX 2000 Ada 8 GB，1024×1024 墙钟）

| 档位 | 经典配方 | 加速档（6 步） |
|---|---|---|
| 文生图 | ~63 s | **~30 s** |
| 参考图编辑 | ~186 s | **~58 s** |
| 背景移除 | ~87 s | **~56 s** |

> 新机如果显存**不是 8 GB**（比如 12/16/24 GB），配方要重调 ——
> `_server.py` 里的 `RECIPES` 和 `PE_VRAM_NEED`（默认 6700 MiB）都是照 8 GB 定的。
> 显存更大可以调高分辨率 / 步数，但**别直接照搬**，先跑一遍三档计时再说。

---

## 6. 踩坑速查

| 现象 | 原因 / 处置 |
|---|---|
| `[2/8]` 报找不到 nvidia-smi | 没装 NVIDIA 驱动 |
| `[5/8]` torch 装不上 | 网络到不了 pytorch.org。公司网要配代理 |
| 自检 `CUDA 可用` 是 False | torch 装成 CPU 版了。必须带 `--index-url .../whl/cu126` 重装 |
| 工作流报找不到 `UnetLoaderGGUF` | `custom_nodes\ComfyUI-GGUF` 没拷全 |
| 工作流报找不到 `ViggleTurboSigmas` | `custom_nodes\ComfyUI-Viggle-Turbo` 没拷全 |
| 出图特别慢 / 卡住 | 双击 `清理显存.bat`，见 §3.3 |
| 手机连不上 | 右键 `放行出图台端口.bat` → 管理员运行 |
| 双击 .bat 一片乱码 | 别用记事本另存 —— .bat 必须 **GBK + CRLF** |
| 出图发灰发暗 | 编辑档步数不够，cfg 2.5 档**必须 ≥ 20 步** |
| 抠图出来是紫色背景 | **不是 bug**，那是 alpha≈0 的填充色，合成后正常 |

---

## 7. 两条硬边界

1. **许可证是 Qwen Research License（非商用）**。商用要单独谈授权。
2. **这套环境没有任何内容过滤**（实测拒绝率 0%）。边界在你自己手上 ——
   涉及真人照片的身份保持编辑、涉及未成年人的内容，都是硬红线。
"""


def robust_write(path, data):
    """写文件，抗「目标被别的进程占用 / 杀软正在扫」。

    网络盘 + 杀软组合下经常出现：文件明明可读、同目录也能新建，
    但**覆盖写**就 PermissionError（说明有别的进程拿着读锁）。
    策略：重试 3 次 → 还不行就把旧文件改名让路，再写新的。

    返回 True / False。改名的旧文件是 `<名字>.locked-<时间戳>`，不会静默丢数据。
    """
    for _ in range(3):
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            return True
        except PermissionError:
            time.sleep(0.8)
        except OSError:
            time.sleep(0.8)
    try:
        if os.path.exists(path):
            alt = "%s.locked-%d" % (path, int(time.time()))
            os.rename(path, alt)
            print("       [注意] %s 被占用，已改名为 %s 让路。"
                  % (os.path.basename(path), os.path.basename(alt)))
        with open(path, "wb") as fh:
            fh.write(data)
        return True
    except Exception as e:
        print("       [失败] 写 %s 还是不行：%s" % (path, e))
        return False


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.1f %s" % (n, u)) if u != "B" else ("%d B" % n)
        n /= 1024.0


def skip_file(name):
    if name in SKIP_FILE_NAMES:
        return True
    if name.startswith(SKIP_FILE_PREFIX):
        return True
    if name.endswith(SKIP_FILE_SUFFIX):
        return True
    if ".bak-" in name or name.endswith(".bat.bak"):
        return True
    return False


def reqs_without_torch(src_req):
    """从 requirements.txt 生成去掉 torch/torchvision/torchsde 的版本。"""
    out = []
    with open(src_req, "r", encoding="utf-8") as fh:
        for ln in fh:
            s = ln.strip()
            low = s.lower()
            if low.startswith(("torch", "torchvision", "torchsde")):
                continue
            out.append(ln.rstrip("\n"))
    return "\n".join(out) + "\n"


def collect(a):
    jobs = []       # (src_abs, rel_dest, size)
    missing = []

    def add(src, rel):
        if os.path.isfile(src):
            jobs.append((src, rel, os.path.getsize(src)))
        elif os.path.isdir(src):
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
                for f in files:
                    if skip_file(f):
                        continue
                    p = os.path.join(root, f)
                    jobs.append((p, os.path.join(rel, os.path.relpath(p, src)),
                                 os.path.getsize(p)))
        else:
            missing.append(src)

    # --- ComfyUI 整棵树（排除规则见文件头）---
    for root, dirs, files in os.walk(SRC):
        rel_root = os.path.relpath(root, SRC)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and d not in SKIP_TOP_DIRS]
        else:
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        # _styletest 只在 --with-styletest 时整体带
        if rel_root.startswith("_styletest") and not a.with_styletest:
            dirs[:] = []
            continue
        for f in files:
            if skip_file(f):
                continue
            p = os.path.join(root, f)
            # ⚠️ 必须 normpath：顶层 rel_root 是 "."，直接 join 会得到 "ComfyUI\.\main.py"，
            # 拷贝没问题（系统会归一化），但拿它当字典键去核对就会全部报「缺件」。
            rel = os.path.normpath(os.path.join("ComfyUI", rel_root, f))
            jobs.append((p, rel, os.path.getsize(p)))

    # --- 模型 ---
    for rel, _desc, tag in MODELS_REQUIRED:
        add(os.path.join(SRC, rel.replace("/", os.sep)),
            os.path.join("ComfyUI", rel.replace("/", os.sep)))
    for rel, _desc, tag in MODELS_OPTIONAL:
        if tag == "pe" and a.no_pe:
            continue
        if tag == "r64" and a.no_r64:
            continue
        add(os.path.join(SRC, rel.replace("/", os.sep)),
            os.path.join("ComfyUI", rel.replace("/", os.sep)))
    for rel, _desc in MODELS_DIRS:
        add(os.path.join(SRC, rel.replace("/", os.sep)),
            os.path.join("ComfyUI", rel.replace("/", os.sep)))

    # --- 便携 Python ---
    if not os.path.isdir(PORTABLE_PY):
        missing.append(PORTABLE_PY)
    else:
        for root, dirs, files in os.walk(PORTABLE_PY):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                p = os.path.join(root, f)
                jobs.append((p, os.path.join("python", os.path.relpath(p, PORTABLE_PY)),
                             os.path.getsize(p)))

    # --- 生成的辅助文件 ---
    return jobs, missing


def verify_package(dest, a):
    """核对「包里的东西」和「源里现在的东西」是否一致。

    查四类问题：
        缺件     —— 源里有、包里没有
        大小不符 —— 两边都有但字节数不同（拷贝被中断）
        过期     —— 大小一样但**源比包新**（打包后又改过源文件）
        多余     —— 包里有、源里已经没有（源删过文件）
    后两类是「同名同大小但内容变了」的隐患，光看大小查不出来，必须比 mtime。
    """
    jobs, missing = collect(a)
    exp = {}
    for src, rel, sz in jobs:
        exp[rel] = (sz, os.path.getmtime(src))

    # ⚠️ 落盘时 newline="\r\n" 会把 \n 换成 CRLF，比对前必须做同样变换，否则必然误报「过期」
    gen = {
        "安装.bat": INSTALL_BAT.replace("\n", "\r\n").encode("gbk", "replace"),
        "使用说明-新机.md": NEW_MACHINE_DOC.encode("utf-8"),
        "_install_check.py": INSTALL_CHECK_PY.encode("utf-8"),
        "_reqs_notorch.txt":
            reqs_without_torch(os.path.join(SRC, "requirements.txt")).encode("utf-8"),
    }

    print("=" * 68)
    print("  安装包完整性核对")
    print("=" * 68)
    print("  包目录 : %s" % dest)
    if not os.path.isdir(dest):
        print("\n  [失败] 目录不存在 —— 还没打包过？")
        sys.exit(1)

    act = {}
    for root, _dirs, files in os.walk(dest):
        for f in files:
            p = os.path.join(root, f)
            act[os.path.relpath(p, dest)] = p

    absent, size_bad, stale, extra = [], [], [], []

    for rel, (sz, mt) in exp.items():
        p = act.get(rel)
        if p is None:
            absent.append(rel)
            continue
        got = os.path.getsize(p)
        if got != sz:
            size_bad.append((rel, sz, got))
            continue
        # copy2 会保留 mtime，所以 mtime 可比
        if mt - os.path.getmtime(p) > 2.0:
            stale.append(rel)

    for rel, want in gen.items():
        p = act.get(rel)
        if p is None:
            absent.append(rel)
            continue
        with open(p, "rb") as fh:
            if fh.read() != want:
                stale.append(rel)

    exp_all = set(exp) | set(gen)
    for rel in act:
        if rel not in exp_all:
            extra.append(rel)

    print("  源里应带 : %d 个文件（+ %d 个生成文件）" % (len(exp), len(gen)))
    print("  包里实际 : %d 个文件" % len(act))
    print("-" * 68)

    def show(title, items, fmt=lambda x: "     " + x):
        if items:
            print("\n  【%s】%d 个：" % (title, len(items)))
            for it in items[:40]:
                print(fmt(it))
            if len(items) > 40:
                print("     ...还有 %d 个" % (len(items) - 40))

    show("缺件", absent)
    show("大小不符", size_bad, lambda t: "     %s  期望 %d  实际 %d" % t)
    show("过期（源比包新）", stale)
    show("多余（源里已无）", extra)

    if missing:
        print("\n  【注意】源里这些路径不存在（打包时也没带）：")
        for m in missing:
            print("     %s" % m)

    bad = len(absent) + len(size_bad) + len(stale)
    print()
    if bad == 0:
        print("  [通过] 包与源一致，没有缺件 / 大小不符 / 过期。")
        if extra:
            print("         多余的 %d 个文件不影响使用（多半是源里删过的实验残留）。"
                  % len(extra))
        sys.exit(0)
    print("  [不一致] %d 项要处理 —— 重新跑一次打包即可（同名同大小会自动跳过，只补差异）。"
          % bad)
    sys.exit(2)


def main():
    if "--utf8" not in sys.argv:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="gbk", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", help="目标目录，例如 X:\\Qwen21_安装包")
    ap.add_argument("--dry-run", action="store_true", help="只列清单算体积，不落盘")
    ap.add_argument("--no-pe", action="store_true", help="不带提示词扩写模型（省 5.24 GB）")
    ap.add_argument("--no-r64", action="store_true", help="不带旧版 4 步加速 LoRA（省 0.32 GB）")
    ap.add_argument("--with-styletest", action="store_true", help="带上风格验证图（+110 MB）")
    ap.add_argument("--utf8", action="store_true",
                    help="强制 UTF-8 输出（给 AI/管道用；在 GBK 控制台双击时不要加）")
    ap.add_argument("--verify", action="store_true",
                    help="只核对已打好的包与源是否一致（查缺件/大小/过期/多余），不复制")
    a = ap.parse_args()

    if a.verify:
        if not a.dest:
            ap.error("--verify 要同时给 --dest（指向已打好的包）")
        verify_package(os.path.abspath(a.dest), a)
        return

    if not a.dest and not a.dry_run:
        ap.error("必须给 --dest，或者用 --dry-run")

    jobs, missing = collect(a)
    total = sum(j[2] for j in jobs)

    # 生成的辅助文件体积（小，但要算进清单）
    gen = [
        ("安装.bat", len(INSTALL_BAT.encode("gbk", "replace"))),
        ("使用说明-新机.md", len(NEW_MACHINE_DOC.encode("utf-8"))),
        ("_install_check.py", len(INSTALL_CHECK_PY.encode("utf-8"))),
        ("_reqs_notorch.txt",
         len(reqs_without_torch(os.path.join(SRC, "requirements.txt")).encode("utf-8"))),
    ]
    total += sum(g[1] for g in gen)

    print("=" * 68)
    print("  Qwen-Image-2.1 一键安装包 —— %s" % ("DRY-RUN（不落盘）" if a.dry_run else "实际打包"))
    print("=" * 68)
    print("  源目录 : %s" % SRC)
    if a.dest:
        print("  目标   : %s" % os.path.abspath(a.dest))
    print("  文件数 : %d   合计 %s" % (len(jobs) + len(gen), human(total)))
    print("  开关   : PE=%s  r64 LoRA=%s  风格验证图=%s"
          % ("不带" if a.no_pe else "带",
             "不带" if a.no_r64 else "带",
             "带" if a.with_styletest else "不带"))
    print("-" * 68)

    # 分类统计
    MODELS_PREF = os.path.join("ComfyUI", "models") + os.sep
    PY_PREF = "python" + os.sep
    CFY_PREF = "ComfyUI" + os.sep

    def bucket_of(rel):
        if rel.startswith(PY_PREF):
            return "便携 Python"
        if rel.startswith(MODELS_PREF):
            return "models"
        if rel.startswith(CFY_PREF):
            return "ComfyUI 上游+自研代码"
        return "其他"

    agg = {}
    for _s, rel, sz in jobs:
        c, s = agg.get(bucket_of(rel), (0, 0))
        agg[bucket_of(rel)] = (c + 1, s + sz)
    for k in ["models", "ComfyUI 上游+自研代码", "便携 Python", "其他"]:
        if k not in agg:
            continue
        c, s = agg[k]
        print("  %-22s %5d 个文件   %10s" % (k, c, human(s)))
    print("  %-22s %5d 个文件   %10s" % ("生成的辅助文件", len(gen), human(sum(g[1] for g in gen))))
    print("-" * 68)

    # 模型明细
    print("\n  模型明细：")
    for rel, desc, tag in MODELS_REQUIRED + MODELS_OPTIONAL:
        p = os.path.join(SRC, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            print("    [缺失] %-62s %s" % (os.path.basename(rel), desc))
            continue
        if (tag == "pe" and a.no_pe) or (tag == "r64" and a.no_r64):
            print("    [跳过] %-62s %s" % (os.path.basename(rel), desc))
            continue
        print("    [%s] %-62s %s" % ("必带" if tag is None else "可选",
                                     os.path.basename(rel), human(os.path.getsize(p))))
        print("           %s" % desc)

    if missing:
        print("\n  【注意】以下源路径不存在，已跳过：")
        for m in missing:
            print("     %s" % m)

    if a.dry_run:
        print("\n（dry-run 结束，没有写任何文件）")
        return

    dest = os.path.abspath(a.dest)
    free = shutil.disk_usage(os.path.splitdrive(dest)[0] + "\\").free
    if free < total * 1.05:
        print("\n  [失败] 目标盘可用 %s < 需要 %s，先腾空间。" % (human(free), human(total)))
        sys.exit(1)

    os.makedirs(dest, exist_ok=True)

    # 生成辅助文件
    def write_text(rel, text, encoding, newline=""):
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if newline:
            text = text.replace("\r\n", "\n").replace("\n", newline)
        robust_write(p, text.encode(encoding, "replace"))

    write_text("安装.bat", INSTALL_BAT, "gbk", "\r\n")
    write_text("使用说明-新机.md", NEW_MACHINE_DOC, "utf-8")
    write_text("_install_check.py", INSTALL_CHECK_PY, "utf-8")
    write_text("_reqs_notorch.txt",
               reqs_without_torch(os.path.join(SRC, "requirements.txt")), "utf-8")
    print("\n  已生成 安装.bat / 使用说明-新机.md / _install_check.py / _reqs_notorch.txt")

    n = 0
    skipped = []
    for src, rel, sz in jobs:
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # 跳过条件：大小一样 **且** mtime 一样（copy2 保留 mtime，所以可比）。
        # ⚠️ 只比大小是不够的 —— 「同名同大小但内容变了」会被漏掉（改 .bat 很容易撞上）。
        if (os.path.isfile(dst) and os.path.getsize(dst) == sz
                and abs(os.path.getmtime(src) - os.path.getmtime(dst)) <= 2.0):
            continue
        try:
            shutil.copy2(src, dst)
        except (PermissionError, OSError) as e:
            # 目标被占用 —— 改名让路再拷一次
            print("       [注意] %s 写不进去（%s），改名让路后重试。"
                  % (os.path.basename(dst), type(e).__name__))
            try:
                if os.path.exists(dst):
                    os.rename(dst, "%s.locked-%d" % (dst, int(time.time())))
                shutil.copy2(src, dst)
            except Exception as e2:
                print("       [失败] %s 还是拷不进去：%s" % (dst, e2))
                skipped.append(rel)
                continue
        n += 1
        if n % 200 == 0:
            print("  ...已复制 %d 个文件" % n, flush=True)

    if skipped:
        print("\n  [注意] %d 个文件没拷进去（目标被占用）：" % len(skipped))
        for s in skipped[:20]:
            print("     %s" % s)

    print("\n  [完成] %d 个文件 -> %s   共 %s" % (n, dest, human(total)))
    print("  下一步：把整个 %s 目录拷到新机，双击里面的 安装.bat。" % os.path.basename(dest))


if __name__ == "__main__":
    main()
