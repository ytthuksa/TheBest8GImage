# LoRA 与「加速档」—— Qwen-Image-2.1 本地出图

> 2026-09-29 建成并实测。核心结论：**挂上 viggle-turbo 加速 LoRA 后，6 步就能出图，实测比 15 步基线快 2.1 倍，画质不降反升（更锐）。**

---

## 一、30 秒上手

### 出图台（网页）
1. 双击 `启动出图台.bat`
2. 左侧参数区找到 **`LoRA`** 下拉 → 选 `viggle-turbo v0.2.1 r128`
3. 下面的 **`加速档`** 会自动勾上（不用手动开）
4. 再往下是 **`加速档预设`** 下拉 —— 默认就是 `6 步 · 官方推荐`
5. 采样步数 / CFG / 采样器 / 调度器 **自动置灰** —— 这一档由 LoRA 接管，调它们没用
6. 正常填提示词 → 开始生成

出图结果上会多一个绿色的 **`加速`** 标签（形如 `viggle-turbo v0.2.1 r128 · 6 步`）。

### 命令行
```bash
cd /c/ComfyUI

# 加速档（自动挑官方推荐的 v0.2.1 r128，默认 6 步）
./.venv/Scripts/python.exe qwen.py t2i "一只橘猫坐在窗台上" --lora turbo -o cat.png

# 画面里要渲染小字（招牌 / 海报 / 字幕）→ 用官方 8 步档
./.venv/Scripts/python.exe qwen.py t2i "霓虹招牌写着 MOTEL 24H OPEN" --lora turbo --turbo-steps 8

# 参考图编辑也支持
./.venv/Scripts/python.exe qwen.py edit "把背景换成雨夜霓虹东京街头" -r photo.png --lora turbo

# 看有哪些 LoRA 和官方步数预设
./.venv/Scripts/python.exe qwen.py loras
```

`--lora turbo` 是简写，等价于写全名 `--lora Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors`。

---

## 一·五、官方步数预设（2026-09-29 补全）

LoRA 不是一个「固定 6 步」的东西 —— **官方定义了一整套 sigma 表**，步数和表是一体的。
README「Rules that matter」+ 节点 tooltip 给的就是下面这 5 档（都是 **raw / 未 shift** 节点，
`ViggleTurboSigmas` 会按分辨率算 `mu` 再 shift，所以任何分辨率都原样传）：

| 步数 | raw sigma 节点 | 官方评价 | 我们暴露 |
|---|---|---|---|
| 4 | `1.0, 0.75, 0.5, 0.25` | 学生模型的**训练 schedule**，不传 sigmas 也是它。细节和多样性略低 | ✅ |
| 5 | `1.0, 0.875, 0.75, 0.5, 0.25` | v0.2 发布时的启动 schedule。约 **4%** 的提示词布局会漂 | ✅ |
| **6** | `1.0, 0.9375, 0.875, 0.75, 0.5, 0.25` | **官方推荐**（v0.2.1），**0%** 布局漂移 | ✅ **默认** |
| 7 | `1.0, 0.9583, 0.9167, 0.875, 0.75, 0.5, 0.25` | 0% 漂移、比 6 步略锐，官方原话「**不比 6 步明显更好**」 | ✅ |
| 8 | `1.0, 0.9375, 0.875, 0.75, 0.625, 0.5, 0.25, 0.125` | **密集小字专用**：画面里要渲染文字时明显更干净 | ✅ |

**怎么选**：默认 6 步；**要出画面里的文字就选 8 步**；4/5/7 留着备用（4 步是 v0.1 唯一的档）。

**改步数的硬规则**：只能在高噪声端（`1 → 0.875`）均匀切分 ——
构图是在 raw sigma `1 ~ 0.875` 之间决定的（shift 后大约前 5% 噪声），那一段跨太大就会**鬼影 + 布局漂移**；
低噪声端的 `0.875 / 0.75 / 0.5 / 0.25` 是学生模型的训练落点，**动不得**（挪了画面明显变软，
Laplacian 锐度 0.014 vs 0.020）。6 步表就是把 4 步表最噪声那段 `1 → 0.75` 切成三份。

### 实测：6 步 vs 8 步（同提示词、同种子 777，1024×1024，霓虹招牌 `MOTEL 24H OPEN`）

| 档位 | 平均亮度 | 对比度(std) | **细节量(Laplacian 方差)** | 耗时（⚠️ 见下） |
|---|---|---|---|---|
| 6 步 | 34.77 | 46.70 | 747.4 | 42.4 s |
| **8 步** | 37.87 | 49.42 | **1075.9** | 37.2 s |

两张图的 `MOTEL / 24H OPEN` **都拼写正确**，8 步的霓虹灯管内部结构和湿沥青颗粒明显更细。
细节量 **+44%**。

> ⚠️ **那两个耗时不可比**：先跑的 6 步是**冷态**（含模型加载），后跑的 8 步是热态。
> 8 步比 6 步多两次模型调用，理论上是 6 步的 1.33 倍。**别引用这组数字做速度对比** ——
> 这是本机反复踩过的坑（同配置冷跑 81.7 s / 热跑 60.4 s，链式差分必假）。

---

## 二、实测数据（1024×1024，RTX 2000 Ada 8GB，墙钟耗时）

| 档位 | 基线（不挂 LoRA） | 加速档（6 步 + LoRA） | 加速比 |
|---|---|---|---|
| **文生图** | 15 步 / res_multistep / cfg 1.0 → **63.1 s** | 6 步 / euler / viggle sigma → **30.1 s** | **2.10×** |
| **参考图编辑** | 20 步 / res_multistep / cfg 2.5 → **188.6 s** | 同上 → **58.2 s** | **3.24×** |
| **背景移除** | 15 步 / res_multistep / cfg 1.0 → **~87 s** | 同上 → **56.1 s** | **~1.55×** |

> 文生图那组是各跑两遍取**热态**（第一遍含 ~17 s 模型加载，不具可比性）。
> 编辑档之所以加速比最高，是因为它基线用的是 cfg 2.5 —— **cfg>1 时每一步要跑两次模型**，
> 而加速档用 `BasicGuider`（无 CFG，单次模型调用），等于同时省掉了步数和每步成本。

### 画质：没有变差，是变锐

同一提示词、同一种子（42），三种跑法的图像统计：

| 跑法 | 平均亮度 | 对比度(std) | **细节量(edgevar)** | 结论 |
|---|---|---|---|---|
| 基线 15 步 | 127.6 | 61.2 | 526 | 参考 |
| **加速档 6 步 + LoRA** | 135.5 | 63.9 | **786** | **比基线更锐** |
| 6 步但**不挂** LoRA（对照） | 125.3 | 60.2 | 255 | 糊、塑料感 |

- 加速档与「不挂 LoRA」的像素平均差 **14.7/255** —— 完全是两张不同的图，证明 **LoRA 真的在生效**，不是被静默忽略。
- 编辑档同 prompt 同 seed 对比：对比度基本持平（48.2 vs 47.6），加速档略亮、更锐（edgevar 686 vs 433），饱和度略低（33.7 vs 36.8）。属**风格差异，不是缺陷**。
- 抠图档：加速档与基线的前景掩膜 **IoU = 0.9857**，透明像素占比 60.9% vs 61.2% —— 掩膜几乎一致。

> ⚠️ 抠图产物是**带 alpha 的 PNG**。用忽略 alpha 的看图软件打开会显示成**紫色噪点背景** ——
> 那是 alpha≈0 区域的填充色，**不是出错**。合成到白底/任意背景后就正常了。
> 基线档同样如此，不是加速档引入的问题。

---

## 三、必须知道的三个限制

### 1. 加速档下 CFG 和负向词**完全不生效**
这是 LoRA 的设计决定的，不是 bug。加速档走 `BasicGuider`（单次模型调用，没有 classifier-free guidance），
负向词根本没被编码进采样。官方 README 也写明：`num_inference_steps=6`，`true_cfg_scale=1.0`，**no negative prompt**。

所以：**要压制某些内容（比如「不要性感化」），加速档下靠负向词是没用的**，只能改正向提示词。
出图台里这一档会把 CFG 滑杆和负向词一起置灰/标注，免得你以为设了其实没设。

### 2. 步数 / 采样器 / 调度器**不能随便改**
viggle-turbo 是 **DMD 蒸馏的少步模型**，它只在特定的 sigma 表上训练过。
用 KSampler 自带的 `simple` / `normal` 调度器会「过拟合锐利」——画面脏、颗粒重。
所以加速档强制走：
- 采样器 `euler`
- 调度器 `ViggleTurboSigmas`（raw nodes，按分辨率自动做 time shift）
- 步数只能从**官方 5 档预设**里选（见第一·五节），不能填任意数字

出图台的「加速档预设」下拉和后端的 `turbo_steps` 参数都会校验 ——
填 9 步、或给 v0.1 r64 填 8 步，都会直接报错，不会静默按错表跑。

> ⚠️ 加速档下外部传的 `steps` 会被**忽略**（以 sigma 表为准），
> 免得 `meta.steps` 和实际采样步数对不上。`qwen.py --lora turbo --steps 20` 会在 stderr 提醒一句。

### 3. 加速档只支持 viggle-turbo 系列
其他 LoRA 会被拒绝（后端直接报错 `加速档只支持 viggle-turbo 系列的 LoRA`）。
普通 LoRA 请**不要勾加速档**，走下面第四节。

---

## 四、挂其他 LoRA（不走加速档）

出图台的 LoRA 下拉里，非 viggle 的 LoRA 会归到「其他 LoRA（合并进权重）」分组。
选中后不勾加速档，就是标准的 `LoraLoaderModelOnly` 路径 —— 合并进权重，按本档常规参数采样。

```bash
# CLI：挂普通 LoRA，走经典采样
./.venv/Scripts/python.exe qwen.py t2i "提示词" --lora my_style.safetensors --lora-weight 0.8
```

> **LoRA 放哪**：`C:\ComfyUI\models\loras\`。放进去后**重启出图台**（后端启动时扫描目录），
> 刷新页面就能在下拉里看到。CLI 每次调用都重新扫描，不用重启。

### 权重
滑杆范围 `0 ~ 2`，默认 `1.0`。
- **viggle-turbo 保持 1.0**，官方明确要求，改小会破坏 DMD 蒸馏的对齐。
- 普通 LoRA 按需调：风格 LoRA 常用 `0.6~0.9`，太高会污染构图。

---

## 五、为什么不用官方自带的 LoRA 节点？

ComfyUI 原生有 `LoraLoaderModelOnly`，它是把 LoRA **合并进权重**（`W' = W + BA`）。
对 viggle-turbo 来说这条路是**有损的**，官方 `viggle_turbo.py` 的文档字符串写得很清楚：

> on bf16 weights round-to-nearest keeps only ~70% of this LoRA's update,
> on int8 weights the stochastic requantization keeps all of it but adds noise ~4x its size.

翻译：bf16 权重下**只能保住约 70% 的更新量**；int8 权重下能保住全部，但会**加进约 4 倍于更新量本身的噪声**。

所以官方节点走**运行时旁路**（unmerged，和 diffusers/PEFT 不调 `fuse_lora()` 一样）：

```
y = W·x + B·A·x
```

用 `register_forward_hook` 在每次前向时临时加上旁路分支，跑完立刻移除（`hk.remove()`），
**不修改任何权重**，所以对共享同一份模型的其他任务零影响。

代价是**每步多花 10~25% 时间**（实测文生图：30.1 s 挂 LoRA vs 27.1 s 不挂，**+11%**），
换来的是完整的 LoRA 效果。这笔账在 6 步的加速档下非常划算。

### 我们的底座是 GGUF，为什么也能挂？

这是本次集成最关键的一个确认点。模型检测（`comfy/model_detection.py:963`）看到
`transformer_blocks.N.img_mlp.gate_up.weight` 就判定为 `qwen_image21` 路径 + `fused_mlp=True`，
对应的实现是 `comfy/ldm/qwen_image21/model.py` 的 `SwiGLUFeedForward(fused=True)`：

```python
if fused:
    self.gate_up = Linear(dim, 2*hidden_dim)   # [gate; up] 融合成一个 GEMM
    ...
    return linear_input_act(self.out, self.gate_up(x), "swiglu")
```

而 LoRA 文件里给的是**分立的** `img_mlp.gate_layer` / `img_mlp.proj` / `img_mlp.out`。
官方节点的 `run_with_lora` 正好按这个组合写：

- `gate_layer` / `proj` 两个键**不单独挂 hook**（`leaf != "out"` 且模块 `fused=True` 时跳过）
- 只对 `out` 键触发 `add_mlp_hooks`，它把 LoRA 的 gate/proj 两支**拼起来加在融合后的 `gate_up` 输出上**，
  再把 down 分支加到 MLP 输出上

所以「GGUF 融合权重 + LoRA 分立键名」这个组合**本来就在官方节点的设计里**。
实测 227 个 LoRA 张量对，全部走通，无一张量被静默丢弃。

---

## 六、技术细节备查

| 项 | 值 |
|---|---|
| LoRA 来源 | `Viggle/Qwen-Image-2.1-viggle-turbo`（HuggingFace） |
| 文件 | `Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors`（648.1 MB） |
| 备选 | `Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors`（324.1 MB，v0.1，留作对照） |
| rank / alpha | 128 / 128（scale = 1.0） |
| 目标模块 | `to_q` `to_k` `to_v` `to_out.0` `img_mlp.{gate_layer,proj,out}` `modulation.1` `time_text_embed.timestep_embedder.{linear_1,linear_2}` |
| 覆盖 | 32 个 transformer block，227 个 A/B 对（454 张量） |
| sigma nodes | `1.0, 0.9375, 0.875, 0.75, 0.5, 0.25`（raw，pipeline 按分辨率做 time shift） |
| time shift | `mu = 0.5 + 0.4·(tokens−256)/(8192−256)`，tokens = 输出 latent 的 H×W（1024² → 4096 → mu ≈ 0.694） |
| 自定义节点 | `custom_nodes/ComfyUI-Viggle-Turbo/`（官方 `viggle_turbo.py`，未改动） |
| 许可证 | ⚠️ Qwen Research License（**非商用**） |

### 工作流结构变化

后端不是另写一套模板，而是**从经典图机械改写**（`_server.py` 的 `_to_turbo()`）：

```
经典：  UNETLoaderGGUF ──────────────────► KSampler ─► VAEDecode ─► SaveImage
                    TextEncodeQwenImage21 ─┘

加速：  UNETLoaderGGUF ─► ViggleTurboLora ─► BasicGuider ─┐
        TextEncodeQwenImage21 ─────────────────────────────┤
        RandomNoise ──────────────────────────────────────┤
        KSamplerSelect(euler) ────────────────────────────┼─► SamplerCustomAdvanced ─► VAEDecode ─► SaveImage
        ViggleTurboSigmas(latent) ────────────────────────┤
        EmptyLatentImage / TE 的 latent ──────────────────┘
```

改写时**不写死节点号** —— 模型源、latent 源、正向条件、种子全部从原 `KSampler` 的输入链接上读。
所以 t2i（latent 来自 `EmptyLatentImage`）和 edit / bgremove（latent 来自 `TextEncodeQwenImage21` 第 3 个输出）
走**同一条变换路径**，三个档位一次搞定。

`_apply_batch`（一次出 N 张）必须在 turbo 变换**之前**跑，因为对 edit/bgremove 它正是靠改
`KSampler.latent_image` 来插 `RepeatLatentBatch` 的 —— 顺序反了就丢。

---

## 七、排障

| 现象 | 原因 / 处理 |
|---|---|
| LoRA 下拉是空的 | `models\loras\` 里没有 `.safetensors`；或后端没重启（启动时才扫目录） |
| 报「加速档只支持 viggle-turbo 系列的 LoRA」 | 选了普通 LoRA 却勾了加速档。取消勾选，或换成 viggle LoRA |
| 报「只支持 4/5/6/7/8 步加速档」 | 步数填了非官方值，或给 v0.1 r64 选了 8 步（它只有 4 步档） |
| 加速档画面**很脏、颗粒重** | 调度器被改过。加速档必须走 `ViggleTurboSigmas`，不能用 KSampler 自带 scheduler |
| 加速档出图**过曝/发白** | 步数被改过。回默认 6 步；要加密只能在高噪声端 |
| 画面里的小字拼错 | 换 **8 步 · 密集文字** 档（官方专治这个） |
| `--steps` 设了没反应 | 加速档下步数由 sigma 表决定，`--steps` 被忽略 —— 用 `--turbo-steps` |
| 加速档负向词没用 | 设计如此，见第三节第 1 条 |
| 报 `ViggleTurboLora` 节点不存在 | `custom_nodes/ComfyUI-Viggle-Turbo/` 没装或 ComfyUI 没重启 |
| 普通 LoRA 挂了没效果 | 权重太低（<0.3）；或该 LoRA 不是 Qwen-Image-2.1 架构的（Z-Image 的 47 个 LoRA 就挂不上） |

---

## 八、相关文件

| 文件 | 作用 |
|---|---|
| `models\loras\*.safetensors` | LoRA 权重 |
| `custom_nodes\ComfyUI-Viggle-Turbo\viggle_turbo.py` | 官方两个节点（未改动） |
| `_server.py` | `TURBO_SCHEDULES`（官方 sigma 表）/ `TURBO_LORAS` 注册表 / `turbo_nodes()` / `list_loras()` / `_to_turbo()` / `_to_plain_lora()` |
| `_web\index.html` | LoRA 下拉 + 权重滑杆 + 加速档开关 + **加速档预设下拉** |
| `qwen.py` | `--lora` / `--lora-weight` / `--turbo` / `--no-turbo` / **`--turbo-steps`** / `loras` 子命令 |
| `_viggle\README.md` | 官方 README 全文（sigma 表、rank 对比、已知限制的原始出处） |
| `_viggle\bench.py` | 三组对照基准脚本（base / turbo / turbo_nolora） |
| `_viggle\test_turbo.py` | 提交任意 API 工作流并计时的小工具 |
| `skills\qwen-image-generate\scripts\verify_lora.py` | 144 项验收（含官方 sigma 表结构、5 档预设、局域网地址） |
