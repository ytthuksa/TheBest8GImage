# TheBest8GImage

**在 8GB 显存的 Windows 本机上跑通 Qwen-Image-2.1 中文出图台** —— 无审查、出图快一倍、209 个预设姿势、软件零花费。

一套跑在 **ComfyUI + Qwen-Image-2.1（8GB 显存 Windows 本机）** 上的**轻量中文 Web 前端**与配套脚本。
双击 `.bat` 即可出图，内置文生图 / 参考图编辑 / 背景移除 / 姿势编辑 / 局部重绘五个标签页，
支持 **双 LoRA 槽位串联叠加**、**VAE 切换**、**7 要素提示词结构面板**、**L0–L3 大胆度档位**，
自带历史画廊与提示词润色。

> ⚠️ **这不是 ComfyUI 的分支，也不是模型仓库。**
> 本项目只是一个**独立进程的前端**，通过 ComfyUI 的 HTTP API（`/prompt`、`/upload/image`、`/free` 等）与之通信，
> 自身不含 ComfyUI 源码、不含任何模型权重。请把它当作「覆盖到你的 ComfyUI 根目录的一叠文件」来用。

---

## ⚠️ 许可证与合规（发布前必读）

本项目能跑起来，依赖三件各自带许可证的东西，**任何一件出商业用途问题都不归本项目管**：

| 组件 | 许可证 | 说明 |
|---|---|---|
| 本前端（本仓库 `_server.py` 等） | **MIT** | 随便用、改、分发 |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | **GPL-3.0** | 你需自行安装，本仓库不包含其代码 |
| [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) | **Apache-2.0** | 自定义节点，自行安装 |
| [ComfyUI-Fisher-Pose](https://github.com/...) | **MIT** | 姿势编辑档所需自定义节点 |
| **Qwen-Image-2.1 底座（GGUF 量化 / 文本编码器 / VAE）** | **Qwen Research License（非商用）** | 🔴 **仅限研究 / 非商用**。商业用途需向通义千问申请授权 |
| VNCCS QI2 PoseStudio LoRA | 未知（随插件发布） | 姿势编辑档必需，运行期加载 |

**结论**：本仓库代码可自由 MIT 分发；但**它驱动出的整套环境（尤其 Qwen 底座）是「非商用」的**。
如果你打算商用，请先把模型换成有商用授权的底座。

> 另外，`ComfyUI-Viggle-Turbo` 加速档插件**没有 LICENSE 文件**（默认保留所有权利），
> 其 LoRA 权重**不得随本项目分发**——本仓库不包含它，需要请自行从原发布方获取。

---

## 功能

| 标签 | 做什么 | 输入 | 1024×1024 实测墙钟耗时（8GB 卡） | 加速档 |
|---|---|---|---|---|
| **文生图** | 纯文字生成图片 | 提示词 + 宽高 | ~63 s | ~30 s |
| **参考图编辑** | 上传一张图，按指令改背景 / 服装 / 姿势，**保住人脸** | 参考图 + 编辑指令 | ~186 s | ~58 s |
| **背景移除** | 上传一张图，抠掉背景 | 参考图（提示词常为空） | ~87 s | ~56 s |
| **姿势编辑** | 摆一个 3D 人偶的姿势，让参考图里的人**照做** | 参考图 + 人偶姿势 | ~150 s | ❌ 不支持 |
| **局部重绘** | 在图上刷蒙版，只重画蒙版区域 | 原图 + 蒙版 + 指令 | **~34 s**（裁剪切块） | ❌ 不支持 |

- 中文深色界面，五个标签页一键出图，参数自动跳到该档实测最优配方。
- **双 LoRA 槽位**：主槽位可放加速档，附加槽位放普通 LoRA，**串联叠加**（实测只多 5~9 s，采样步速不变）。
- **VAE 下拉**：官方 / 模板自带可切换，全档生效。
- **提示词结构面板**：按「机位 → 主体 → 姿势 → 场景与光 → 皮肤 → 显式细节 → 收尾」七要素逐格填，
  自动拼成一段，实时报字符数（作者配方建议 300–1500 字符）并点名还缺哪几格。
- **L0–L3 大胆度档位**：选中后只往提示词末尾追加「机位 + 姿势」两段，不动画风、不动采样参数。
- **历史画廊**：自动留存每次出图，可对比、可下载。
- **提示词润色**：用本机 Ollama 把一句话扩写成英文提示词（零显存占用，不可用时静默回落离线规则）。
- **一键释放显存**：`清理显存.bat` 让 ComfyUI 卸载缓存模型、停掉 Ollama 驻留。
- **局域网访问**：同一 WiFi 下手机 / 平板可直接打开出图台（⚠️ 该模式无密码，详见末节）。

---

## 环境要求

| 项 | 最低要求 | 实测环境 |
|---|---|---|
| 系统 | Windows 10/11（64 位） | Windows 11 |
| GPU | NVIDIA，≥ 8 GB 显存（Compute Capability 8.x 优先） | RTX 2000 Ada Laptop 8 GB |
| Python | 3.13（建议用 ComfyUI 自带的 venv） | 3.13.x |
| 后端 | 已部署 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 0.37.x | 0.37.0 |
| 自定义节点 | `ComfyUI-GGUF`、`ComfyUI-Fisher-Pose`（姿势档需要） | — |
| 可选 | [Ollama](https://ollama.com)（提示词润色，无则自动绕过） | — |

> 本项目**纯标准库实现**（只用到 `http.server` / `urllib` / `subprocess` 等），不额外装包。

---

## 模型文件

请自行下载下列权重，按 ComfyUI 常规子目录放置（文件名需与此完全一致）：

| 文件名 | 放哪 | 约大小 | 用途 |
|---|---|---|---|
| `qwen-image-2.1-Q4_K_M.gguf` | `models/diffusion_models/` | 4.0 GiB | DiT 底座（GGUF 量化） |
| `qwen3vl_8b_w4a8.safetensors` | `models/text_encoders/` | 5.9 GiB | 文本编码器（Qwen3-VL） |
| `qwen_image_2.1_vae_bf16.safetensors` | `models/vae/` | 0.6 GiB | VAE 解码 |
| `VNCCS_QI2_PoseStudioV1.1.safetensors` | `models/loras/` | 0.15 GiB | 姿势编辑 LoRA（强度固定 1.0） |
| `Qwen-Image-2.1-viggle-turbo-*-lora-*.safetensors` | `models/loras/` | 0.3 / 0.6 GiB | 加速档（可选，非必须） |

**可选 LoRA**（全部放 `models/loras/`，出图台会**自动扫描目录**并出现在下拉里，不需要改代码）：

| 用途 | 典型强度 | 说明 |
|---|---|---|
| 编辑一致性 | 1.0 | 参考图编辑档用来压住身份漂移 |
| 换装 | 1.0 | 可叠加速档；真换装需要双图工作流（当前 edit 档只接 1 张图） |
| 相机视角 `<mva> az,el` | 0.8~1.0 | **只在编辑档有效**（从参考图转视角），文生图档会产生同一张图 |
| 链式分镜 Next-Scene | ≤ 0.7 | 强度过高会糊，建议每跳补 0.5px 高斯 |
| 插画转真人 | 0.6~0.8 | — |

> ⚠️ 本仓库**不包含任何 LoRA 权重**。LoRA 各有独立许可证，请自行从原发布方获取，
> 并自行确认其授权范围（尤其涉及成人内容的模型，多数禁止再分发）。

> 提示词润色默认走本地 Ollama（`ollama create qwen-image-pe-t2i -f models\prompt_enhancer\Modelfile` 后可用官方 PE 模型）；
> Ollama 不在时自动回落离线规则，不影响出图。

---

## 安装与启动

1. 把本仓库的文件**原样覆盖到你的 ComfyUI 安装根目录**（与 `main.py` 同级）。
   关键在于 `_server.py` / `qwen.py` 等脚本都用「自身文件所在目录」定位 ComfyUI，因此**必须放在根目录**，
   不能塞进子文件夹。根目录结构见下。

2. 按上表放好模型与自定义节点，确认 `启动ComfyUI.bat` 能正常拉起后端（默认 `http://127.0.0.1:8188`）。

3. 双击 **`启动出图台.bat`** —— 它会自动：清理显存 → 体检 ComfyUI 后端（僵死进程自动清掉）→
   启动前端服务（默认 `http://127.0.0.1:8190`）→ 打开浏览器。

4. 出图台里选标签、填提示词、点出图。停止时双击 **`停止出图台.bat`**。

> 命令行党可直接用 `qwen.py`：
> `python qwen.py t2i "一只戴帽子的猫"`、`python qwen.py edit -r 图.png "换成雨夜东京"`、
> `python qwen.py bgremove -r 图.png`、`python qwen.py enhance "赛博朋克猫"`；
> `python qwen.py loras` / `vaes` / `styles` / `list` / `status` 查可用资源。
>
> 常用通用选项：`--lora`（主槽位）/ `--lora2`（附加槽位，串联叠加）/ `--vae` /
> `--boldness L0~L3`（大胆度档位）/ `--sampler` / `--scheduler` / `--steps` / `--cfg`。
> 支持模糊匹配，例如 `--lora2 consistency`、`--vae official`。
> 成功时 stdout 末行是 `OUTPUT: <绝对路径>`。
>
> 姿势编辑档需要浏览器里摆 3D 人偶，CLI 做不了，请用出图台。

### 局域网 / 防火墙

- `启动出图台.bat` 默认监听 `0.0.0.0`，同 WiFi 的手机可直接访问（地址会打印在控制台，也会显示在网页右上角胶囊里）。
- 手机打不开 → 右键 **`放行出图台端口.bat`** → 以管理员身份运行一次（放行 8190 端口）。
- 只想本机用：双击 `启动出图台-仅本机.bat`（`WEB_HOST=127.0.0.1`，网页不再显示局域网地址）。

---

## 提示词工具：7 要素结构 + 大胆度档位

出图台把「怎么写提示词」做成了两个可视化控件，**都只改提示词，不动任何采样参数**。

### 7 要素结构面板

提示词框下方点 **`🧩 结构`**，按固定顺序填七格：

| 顺序 | 要素 | 填什么 |
|---|---|---|
| 1 | 机位 | 镜头 + 角度（`low-angle shot, 35mm lens`） |
| 2 | 主体 | 年龄 / 体型 / 发型状态 |
| 3 | 姿势 | **逐肢交代**：哪条腿弯、手在哪、视线看哪 |
| 4 | 场景与光 | 地点 + 光线 |
| 5 | 皮肤 | 写实感全靠它（`glistening` / `visible pores`） |
| 6 | 显式细节 | 可选；泛词（`seductive` 之类）几乎无效 |
| 7 | 收尾 | 固定收尾句，别删 |

点「拼成提示词」→ 按上述顺序拼成一段写进提示词框，并报字符数与缺失项。
**短于 300 字符会提示「偏平淡」** —— 短提示词是出图平淡的头号原因。

### L0–L3 大胆度档位

选中档位后，只往提示词**末尾**追加该档的「机位 + 姿势」两段英文：

| 档 | 用途 |
|---|---|
| L0 | 全着装 / 编辑感 |
| L1 | 泳装 / 湿身 / 放松 |
| L2 | 大胆（低机位 + 舒展姿势） |
| L3 | 极端 / 特写构图 |

> ⚠️ **追加在末尾而不是开头**：七要素的自然顺序是「机位 → 主体 → 姿势」，放开头会打乱结构。
> 因此**已经用结构面板写全七格的，就别再选档位** —— 两个姿势描述会被模型混在一起。
> 界面在检测到这种情况时会主动提醒。
>
> 三条实测经验：低机位 ≈ 大胆度 +1 档；一个提示词只写一个姿势；
> 着装档（L0）配成人向 LoRA 是最常见的「出图平淡」组合。

---

## 目录结构

```
ComfyUI\                      ← 你的 ComfyUI 根目录（本仓库覆盖到这里）
├── _server.py                出图台后端（纯标准库 HTTP 服务，端口 8190）
├── _web\index.html           前端单文件页面
├── qwen.py                   CLI 出图门面（t2i / edit / bgremove / list / styles）
├── _prompt_eng.py            提示词风格库 + Ollama 润色 / 离线回落
├── _api_t2i.json            文生图 API 工作流模板（喂给 ComfyUI /prompt）
├── _api_edit2.json          参考图编辑模板
├── _api_bgremove_cfg1.json  背景移除模板
├── _api_pose.json           姿势编辑模板
├── _api_inpaint.json        局部重绘模板（全幅）
├── _api_inpaint_crop.json   局部重绘模板（裁剪切块，默认）
├── _web\_i18n.js            中英双语词条（zh / en 各一套 key）
├── _free_vram.py            释放显存（ComfyUI /free + Ollama）
├── _release_resources.py    释放系统资源（一键关机前的清理）
├── _zombie_check.py         端口体检：端口在监听 ≠ 服务活着
├── _apply_zombie_guard.py   给启动脚本加僵尸检测守卫
├── 启动出图台.bat / 停止出图台.bat / 启动ComfyUI.bat
├── 启动出图台-仅本机.bat / 启动出图台-局域网.bat
├── 清理显存.bat / 释放资源.bat / 放行出图台端口.bat
├── workflows\               ComfyUI 原生界面用的四个工作流（可选，放进 user/default/workflows/）
│   ├── qwen_image_2_1_local_t2i.json
│   ├── qwen_image_2_1_local_edit.json
│   ├── qwen_image_2_1_local_bgremove.json
│   └── qwen_image_2_1_local_pose.json
├── docs\                    使用说明 / 加速档 / 风格模板（从开发环境导出的文档）
│   ├── 使用说明.md
│   ├── LoRA加速档.md
│   └── 风格模板.md
└── （开发工具：_make_installer.py / _check_*.py / _validate_ui_workflow.py，仅维护用，不影响运行）
```

> `docs/` 里引用到的 `_styletest\`、`_wx\` 等是开发期脚手架，**未包含在本仓库**。

---

## 8GB 显存避坑

- ComfyUI 出图后**不会主动释放**缓存模型（DiT + LoRA + VAE ≈ 3.9 GiB）。再想出图却报显存不足时，
  先怀疑它：看 `/system_stats` 的 `torch_vram_total` 是否 > 0。调用 `POST /free {"unload_models":true,"free_memory":true}`
  即可释放（该接口返回空 body，只看 HTTP 状态码）。
- ComfyUI 与 Ollama-GPU **不能同时全力跑**；出图前先停掉 Ollama 驻留（`清理显存.bat` 会一起处理）。
- 同一参数重复提交会命中 ComfyUI 执行缓存（0.3 秒「完成」返回上次的图），**对比测试务必换种子**。
- **挂 LoRA 会让每一步变慢**：实测 768² / cfg 1.0 下，无 LoRA 2.13 s/it，带 LoRA 2.94 s/it（**+38%**）。
  这是 `LoraLoaderModelOnly` 的运行时 patch 开销，不是 bug，但排期要算进去。
- **显存大头是固定的模型权重，不是像素**：1728² 比 768×1152 只多约 700 MiB。
  别按像素数线性外推显存占用。

---

## 隐私与安全

- **局域网模式下出图台没有任何认证**，同一个 WiFi 里的任何人都能用你的显卡出图。
  **不要在公共 WiFi / 开放网络下用 `0.0.0.0` 监听**；公共场合请改用 `启动出图台-仅本机.bat`。
- 你放进 `models/loras/` 的所有 LoRA（包括成人向模型）**都会出现在局域网访问者的下拉里**，
  「显示成人向」勾选框只过滤风格预设与大胆度档位，**不隐藏 LoRA 列表**。
  介意的话请在共享网络下改用仅本机模式。
- 所有计算都在本机完成，提示词和图片**不会上传到任何云端**（Ollama 润色若有，也是本机推理）。

---

## 许可证

- 本仓库代码：**MIT**（见 `LICENSE`）。
- 依赖项许可证见上文表格，商业用途请先确认 Qwen 底座的授权范围。

---

## 致谢

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 与社区自定义节点提供了推理后端。
- Qwen 团队开源的 Qwen-Image-2.1 系列模型。
- Fisher Pose / VNCCS 提供的姿势编辑 LoRA 与节点。
