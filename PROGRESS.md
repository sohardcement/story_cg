# PROGRESS

> 晨报（TASK.md §10）将在收尾时写在本节之上。

## 当前阶段

- P0 环境核对：监工亲自完成（见下文「P0 环境报告」）。
- P0-01 冒烟通过：沙盒 `workspace-write + network_access=true` 下可访问 ComfyUI、模型服务、Danbooru，可写工作目录。
- P1-01 通过：项目骨架 `src/storycg/`，先落盘再提交、SIGKILL 后 resume 无重复提交（证据 `runs/p1_01_smoke_20260930/`）。
- P1-02 通过 P1 门槛（真实图片+自评）：芙莉莲 4 镜，身份/服装稳定，但只看图读不出剧情（伞无法区分、构图雷同、仍在下雨）。证据 `review/芙莉莲-下雨后在车站发现自己拿错了伞.jpg`。
- **P1 阶段门槛：通过**（监工判定）。叙事可读性问题转入 P1-04 与 P5。
- P1-03 通过（返工 1 次）：三层审核器初版，盲审请求经监工读日志与代码确认不含剧情/分镜/角色名。
- P1-04 部分通过：镜头 tag 化、道具登记、景别映射、场景负面词已实现并见效（对照 `review/P1-02-vs-P1-04.jpg`）；但只看图仍读不出剧情，道具数量/持有关系靠纯文本无法可靠绑定。
- **P2 阶段门槛：通过**（监工判定）。选定 WAI v170 + Iwao178 0.4 + akaimelon 0.3，euler_ancestral / normal / 28 步 / CFG 5.5，写入 `config/style.json`。理由：更高权重会让瞳色偏离原设并产生多余肢体残片（证据 `verify/p2-Bfaces.jpg`）。
- 下一步：P3-01（定妆图 + IPAdapter / Qwen Edit 试点）。

## 占位项的取值（TASK.md 中未填写的【】）

用户交代"全程不要等我回复"，以下按最保守、可逆的原则取值，已列入「待决」请用户确认：

| 占位 | 取值 | 理由 |
|---|---|---|
| 工作目录路径 | `/work/workspace/manga_skill`（TASK.md 所在目录，启动时只有 3 个 md 文件） | 新目录、已 git init |
| 现有 skills 路径 | `~/.config/magpie/library/skills/`（`~/.claude/skills`、`~/.agents/skills` 中的条目是指向它的符号链接）与 `~/.hermes/skills/` | 只读参考基础设施信息 |
| 自定义节点/模型下载 | **A：禁止** | 最保守；缺什么写进待决 |
| 每组耗时上限 | 45 分钟 | TASK.md 已给默认值 |
| 开发期总出图上限 | 400 张 | TASK.md 建议值 |
| Codex 退避单次最长等待 | 60 分钟 | SUPERVISOR.md 已给默认值 |

## P0 环境报告（监工亲自核对，2026-09-30）

证据：`verify/object_info.json`（`/object_info` 原始响应，5.3 MB，2326 个节点）。

### ComfyUI

- 地址 `http://127.0.0.1:8188`（来自现有 skills，已重新验证）。`/system_stats`：ComfyUI 0.36.0，Windows，RTX 4070 SUPER 12 GB，PyTorch 2.11+cu130。
- `/object_info` 首次响应 44 秒（服务端冷启动慢）；客户端超时需 ≥180 秒。
- **ComfyUI 是共用的**：核对时队列里正有别人的任务在跑（`story_cg_portrait_*`，旧 skill）。不得清空队列、不得 interrupt 别人的任务；我们的任务要按 prompt_id 追踪。
- 无认证。

### 画风资源（WAI / Iwao / akaimelon）

- WAI checkpoint：`waiIllustriousSDXL_v170.safetensors`（CheckpointLoaderSimple 唯一的 checkpoint）。`/view_metadata/checkpoints` 返回 404，无内嵌哈希。
- Iwao：候选 `Iwao178.safetensors`（元数据仅 Author=eTchola，无哈希、无训练底模信息）、`iwaoCIVITAI.safetensors`、`iwao_validation\*`（验证用中间产物）、`iwao_style_Anima_v40.safetensors`（Anima 底模专用，不适用 SDXL）。
- akaimelon：候选 `akaimelon.safetensors`（sshs_model_hash `700403771cb0…c8e209`，SDXL，dim 32，触发词 `akaimelon`；**训练集以 NSFW 为主**，全年龄输出需负面提示词压制）、`akai-melon-sdxl-lora.safetensors`、`Akaimelon（spyxfimaly）.safetensors`、`akai_melon_style_Anima_v40.safetensors`（Anima 专用）。
- 现有 story-cg-director 的描述写的是"WAI + Iwao178 + akaimelon"。P2 以 `Iwao178` + `akaimelon` 为主候选，其它 SDXL 版本作为网格对照，不静默更换。
- 哈希：远端是 Windows 机器，无法直接读文件算 SHA256。可用来源：safetensors 元数据中的 `sshs_model_hash`（若有）、`/experiment/models/<folder>` 返回的 size/mtime。manifest 中按"可得即记，不可得标注 unavailable"处理。

### 角色 LoRA（L1 候选）

- `Fern_Frieren_Beyond_Journeys_End_-_Illustrious_V2.safetensors`（菲伦；sshs `46066eb9…14ac65`；训练 tag 含 `fern (sousou no frieren)`、`purple hair`、`purple eyes`）
- `tsukishiro_yanagi_zzz_lora_finished.safetensors`、`yanagizzz_v4_best_step1650.safetensors` 等（绝区零 月城柳）

### 身份控制相关节点与模型

| 类别 | 可用 | 模型 |
|---|---|---|
| 参考注入 IPAdapter（SDXL） | IPAdapter 全家（IPAdapterAdvanced、UnifiedLoader…） | `ip-adapter-plus-face_sdxl_vit-h.safetensors` + CLIP-ViT-H（`CLIP-ViT-H-14-laion2B-s32B-b79K`）。**没有** 非 face 版 plus/standard SDXL 模型；FaceID 需要 insightface（未见模型）|
| 参考编辑 Qwen Image Edit | `TextEncodeQwenImageEditPlus`、`UnetLoaderGGUF` | `qwen-image-edit-2511-Q4_K_M.gguf`、`Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf`/`qwen_2.5_vl_7b_fp8_scaled`、`qwen_image_vae.safetensors`、Lightning 4-step LoRA `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16` |
| Inpaint | SetLatentNoiseMask、InpaintModelConditioning、VAEEncodeForInpaint、Fooocus inpaint | `inpaint_v26.fooocus.patch`、`fooocus_inpaint_head.pth` |
| ControlNet | ControlNetApplyAdvanced 等 | `OpenPoseXL2.safetensors`、`control-lora-openposeXL2-rank256`；**没有** 姿态检测预处理器（无 controlnet_aux / DWPose 预处理节点）|
| 面部检测/裁切 | MediaPipeFaceLandmarker、CropByBBoxes、ImageCrop | **没有检测模型**：`LoadMediaPipeFaceLandmarker` 模型列表为空，`easy ultralyticsDetectorPipe` 模型列表为空，无 Impact Pack FaceDetailer/SAMLoader |
| 放大 | UpscaleModelLoader | `RealESRGAN_x4plus_anime_6B.pth` 等 |

结论：身份控制可走 IPAdapter plus-face（SDXL 原生）或 Qwen Image Edit（以定妆图为参考构图，再用 WAI 重绘）；局部重绘可用。面部检测需在 ComfyUI 外解决（例如让视觉模型给出面部 bbox，本地裁切），这一点在 P3/P7 前需验证。

### 视觉模型服务 / Hermes 当前模型

- `http://127.0.0.1:11234/v1`（OpenAI 兼容，mlx-serve），模型 `Qwen3.8-Flash-Next-Uncensored-MLX-Serve-4bit`，capabilities 含 vision / json_schema / reasoning，**上下文 262144**。
- 不带 API key 可调用。实测一次发 3 张图（红/蓝/绿纯色）→ 返回 `["red","blue","green"]`，3.5 秒。**支持多图输入**。
- 这也是 Hermes 当前使用的模型（`~/.hermes/config.yaml` model.default，provider mlx-server）。审核器和运行时 LLM 是同一个模型——"自己出题自己考"的风险由监工在 P4 把关。

### Hermes

- `hermes --version`：Hermes Agent v0.21.5（2026.9.24），安装目录 `~/.hermes/hermes-agent`，Python 3.14.7。
- skill 加载：`~/.hermes/skills/` 为主目录（可按 category 分子目录），另有 external_dirs `~/.agents/skills`。每个 skill 是含 `SKILL.md`（frontmatter name/description）的目录，自动成为 `/skill-name` 斜杠命令。非交互调用：`hermes -z "<prompt>" --skills <name>`。
- 安装计划：P7 时新建 `~/.hermes/skills/<新名字>/`（不覆盖已有 skill，不改全局配置）。

### 角色资料源

- Danbooru 可访问：`/wiki_pages/<tag>.json`、`/tags.json`、`/posts.json` 均 200。
- `danbooru.donmai.us/autocomplete.json?search[query]=菲伦&search[type]=tag_query` 直接返回 `fern_(sousou_no_frieren)`（antecedent 为中文别名"菲伦"）——可用于中文名消歧。
- 中文维基 API 可访问；萌娘百科 API 返回 Unauthorized。

### 本机环境

- 系统 `python3` 3.14.7，**无 Pillow**；有 `uv`、ImageMagick (`magick`)。
- codex-cli 0.159.2，已登录（ChatGPT）。用户 config 默认 `sandbox_mode = danger-full-access`，派工时必须显式 `--sandbox workspace-write`（`exec resume` 无 `--sandbox`，用 `-c sandbox_mode="workspace-write"`）。

## 决策记录

- D1 占位项取值见上表。
- D3 画风：Iwao178 0.4 / akaimelon 0.3（P2-01，身份优先取最低档）。
- D4 Codex 派工加 `-c memories.use_memories=false`，避免 Codex 全局记忆中的旧 story-cg 项目内容渗入（TASK.md §2）。
- D2 Codex 调用：`codex exec -C <workdir> --sandbox workspace-write -c sandbox_workspace_write.network_access=true --json -`；返工 `codex exec resume <id> -c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=true --json -`。

## 工单列表

| 工单 | 结论 | 返工次数 | 会话 ID | 证据 |
|---|---|---|---|---|
| P0-01 冒烟 | 通过 | 0 | 01a0f225-82be-7381-91df-8a7802b4eb8e | `smoke/*.json`、`reports/P0-01.md`；监工复核 JSON 合法、vlm_chat 内容为 pong |
| P2-01 画风网格 | 通过 | 0 | 01a0f2e5-502f-7a61-8ec4-260891c14059 | `review/P2-grid-A.jpg`、`review/P2-grid-B.jpg`、`verify/p2-Bfaces.jpg` |
| P1-04 镜头 tag 化+道具登记+景别映射 | 部分通过（叙事未达成） | 0 | 01a0f2c1-6126-7300-968b-7e7f9699ed4e | `review/P1-02-vs-P1-04.jpg`、`review/芙莉莲-拿错伞-P1-04.jpg` |
| P1-03 三层审核器初版 | 通过 | 1 | 01a0f2a7-d180-78a3-b8f6-6343c002c0ca | `runs/p1_02_frieren_20260930/review.json`、`review_log.jsonl`、`review_artifacts/face_pair_*.png` |
| P1-02 身份锁定+分镜+4镜+拼图 | 通过（叙事未通过，见下） | 0 | 01a0f24c-d0ce-7d61-8cc1-b5cb3168394e | `review/芙莉莲-下雨后在车站发现自己拿错了伞.jpg`、`runs/p1_02_frieren_20260930/` |
| P1-01 骨架+单镜出图+恢复 | 通过 | 0 | 01a0f22a-043d-7a33-a1e8-21cf70e6ec4c | `runs/p1_01_smoke_20260930/`；pytest 16 passed；/history 同 prefix 仅 1 条 |

## 出图用量（开发期上限 400）

| 工单 | 出图张数 | 审核调用 | 累计 |
|---|---|---|---|
| P0-01 | 0 | 0（文本 1） | 0 |
| P1-01 | 1 | 0 | 1 |
| P1-02 | 4 | 0（LLM 5） | 5 |
| P1-03 | 0 | 14 | 5 |
| P1-04 | 8 | 14（LLM 6） | 13 |
| P2-01 | 20 | 0 | 33 |

## 审核器与监工判断不一致记录

| 来源 | 审核器结论 | 监工看图结论 | 说明 |
|---|---|---|---|
| P1-03 对 P1-02 组 | 整组未通过；盲审未读出"拿错伞" | 一致 | 叙事层判对 |
| P1-03 对 P1-02 组 | 场景连续性 4 镜全 pass | 不一致：列车颜色/车型每镜都变 | 场景连续性漏判；同一次调用在盲审里自己描述了列车变化 |
| P1-03 对 P1-02 组 | 第 3 镜 hands pass、prop_contact minor | 不一致：头顶开伞无可见握持 | 手-道具接触漏判 |
| P1-03 对 P1-02 组 | 第 1/2/4 镜构图"符合 medium" | 不一致：实际近全身 | 景别判断不可靠 |
| P1-04 组 | 第 4 镜 action/prop_contact pass | 不一致：两把伞都在手里，没有放下红伞 | 持有关系漏判（第二次出现） |
| P1-03 对 P1-02 组 | 身份/服装多数 pass | 基本一致（参考是第 1 镜，不是定妆图） | 仅能说明组内相对一致 |

## 阻塞项

（无）

## 待决

1. TASK.md 中的【】占位未填写，已按上表取保守值继续，请确认（尤其是节点/模型下载选择了 A 禁止）。
