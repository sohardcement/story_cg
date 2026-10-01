# PROGRESS

## 晨报（草稿，持续更新；最后更新 2026-10-01 07:20，P7 进行中）

### 各阶段状态

| 阶段 | 状态 | 证据 |
|---|---|---|
| P0 环境核对 | 通过 | 本文「P0 环境报告」；`verify/object_info.json` |
| P1 最小闭环 | 通过 | `review/芙莉莲-下雨后在车站发现自己拿错了伞.jpg`、`runs/p1_02_frieren_20260930/` |
| P2 画风定参 | 通过 | `review/P2-grid-A.jpg`、`review/P2-grid-B.jpg`、`config/style.json` |
| P3 身份控制 | 部分通过：IPAdapter 0.4 在芙莉莲上改善成立；约尔首轮因背影无法判断，P5-02 中正脸可见且一致 | `review/P3-01-pilot.jpg`、`review/P3-02-芙莉莲-对照.jpg` |
| P4 审核器校准 | 通过 | `calibration/summary.md`、`config/review_reliability.json` |
| P5 自动修复 | 部分通过：循环可用；约尔组只看图可读出完整故事，但超时 4 秒且盲审超时；芙莉莲组叙事仍含糊 | `review/P5-02-约尔-前后.jpg`、`review/P5-02-芙莉莲-前后.jpg` |
| P6 通用角色流程 | 部分完成：消歧、原设补全、L1/L2 判定已实测；L3/L4 未做（P6-02 待派） | `review/P6-01b-*.jpg`、`runs/p6_01a_resolve/05`（歧义名候选） |
| P7 Hermes 集成 | 进行中（P7-01） | — |
| P8 测试矩阵 | 未完成：已有 2 组（芙莉莲-拿错伞、约尔-追帽子） | `verify/P8-matrix-plan.md` |

### 已验证的能力

- 先落盘再提交、按 prefix 对账恢复：SIGKILL 后恢复无重复提交（P1-01）；ComfyUI 宕机 20 分钟后恢复，宕机前已提交的任务经 history 对账直接下载，未重投（P5-01b）。
- 中文名消歧：`爱丽丝` 返回 8 个候选要求提问；`芙莉莲`/`约尔`/`菲伦`/`月城柳` 唯一解析（P6-01a）。
- 原设资料带来源：wiki + Danbooru solo 共现频率，每个 tag 记录 URL（P6-01a，监工抽查一致）。
- L1（角色 LoRA 由训练 tag 元数据匹配）/L2（无参考测试图 + 筛选参考比对）判定，四个角色测试图监工确认是本人（P6-01b）。
- 身份控制：定妆图先过审，IPAdapter 0.4 注入定妆面部；把基线的蓝瞳纠正为绿瞳（P3）。
- 审核器在发色、服装、道具、动作四项上可靠（P4 校准，检出 89–100%，误报 0%）。
- 盲审请求只含图片（监工读日志与代码确认）。

### 仍有限制的能力

- **瞳色、配饰、整体角色的审核不可靠**（检出 33–67%），交付说明会标"未经可靠审核"，需要人看图。L1/L2 判定的可靠依据实际只有发色。
- 区域重绘未验证成立（视觉定位物体不可靠），默认关闭；道具问题靠 Qwen 指令编辑/重布局，仍常出现**重复物体**。
- "拿错东西"一类需要读出归属的剧情仍读不出来（芙莉莲组）。
- 4 镜一组含修复约 42–45 分钟，贴着 45 分钟上限；盲审（4 张全图）偶发超时。

### 最值得看的拼图

1. `review/P5-02-约尔-前后.jpg` —— 左 P3-02、右 P5-02 自动修复循环，右侧是目前最好的一组（故事完整可读）。
2. `review/P3-01-pilot.jpg` —— 身份控制机制对照（无参考 / IPAdapter 0.4 / 0.7 / Qwen 布局）。
3. `review/P4-samples.jpg` —— 审核器校准样本（配合 `calibration/summary.md`）。
4. `review/P6-01b-芙莉莲.jpg` —— L2 判定的测试图与参考对照。
5. `review/P2-grid-A.jpg` —— 画风 LoRA 权重网格。

### 待我决定的问题

见文末「待决」。

### 如何在 Hermes 中调用 / 如何恢复

（P7 完成后补）

## 当前阶段

- P0 环境核对：监工亲自完成（见下文「P0 环境报告」）。
- P0-01 冒烟通过：沙盒 `workspace-write + network_access=true` 下可访问 ComfyUI、模型服务、Danbooru，可写工作目录。
- P1-01 通过：项目骨架 `src/storycg/`，先落盘再提交、SIGKILL 后 resume 无重复提交（证据 `runs/p1_01_smoke_20260930/`）。
- P1-02 通过 P1 门槛（真实图片+自评）：芙莉莲 4 镜，身份/服装稳定，但只看图读不出剧情（伞无法区分、构图雷同、仍在下雨）。证据 `review/芙莉莲-下雨后在车站发现自己拿错了伞.jpg`。
- **P1 阶段门槛：通过**（监工判定）。叙事可读性问题转入 P1-04 与 P5。
- P1-03 通过（返工 1 次）：三层审核器初版，盲审请求经监工读日志与代码确认不含剧情/分镜/角色名。
- P1-04 部分通过：镜头 tag 化、道具登记、景别映射、场景负面词已实现并见效（对照 `review/P1-02-vs-P1-04.jpg`）；但只看图仍读不出剧情，道具数量/持有关系靠纯文本无法可靠绑定。
- **P2 阶段门槛：通过**（监工判定）。选定 WAI v170 + Iwao178 0.4 + akaimelon 0.3，euler_ancestral / normal / 28 步 / CFG 5.5，写入 `config/style.json`。理由：更高权重会让瞳色偏离原设并产生多余肢体残片（证据 `verify/p2-Bfaces.jpg`）。
- P3-01 通过：定妆门槛+缓存实现；试点中 IPAdapter 0.4 修正了基线的蓝瞳（身份改善），0.7 画风崩坏；Qwen 布局对道具位置最有效但慢且会多画道具。监工选定 IPAdapter 0.4 为默认身份控制（D5）。
- P3-02 通过：正式 CLI 支持 `--identity-control none|ipadapter` 与 `--layout-mode none|qwen`。芙莉莲：IPAdapter 0.4 身份改善成立（蓝瞳→绿瞳）；Qwen 布局改善道具空间关系但会多画道具。约尔：分镜多为背影，身份改善无法判断。
- **P3 阶段门槛：部分通过**（监工判定）。身份控制机制已确定且在一个角色上证明有效；第二角色待 P6 修好身份资料、分镜保证露脸后复验。
- P4-01 通过：22 个编号样本，监工逐张确认后写成逐项真值 `verify/P4-labels.json`（剔除 E1b）。
- P4-02 通过：审核器修正后冻结并校准。可靠：发色、服装、道具、动作；不可靠：瞳色、配饰、整体角色；未验证：发型。
- **P4 阶段门槛：通过**（监工判定）。
- P5-01 部分通过：面部精修有效（瞳色纠正）；区域重绘两例均失败（mask 选区错误、方图压扁接缝）；单镜重布局因 ComfyUI 宕机未完成。
- ComfyUI 宕机约 20 分钟（02:46–03:06），期间完成 P6-01a（名字消歧、原设补全、默认服装选择，不出图）。
- P5-01b 部分通过：宕机前提交的任务经对账恢复、未重复提交（真实中断恢复成立）；重布局提升可读性但会复制物体；区域重绘因视觉定位不可靠未验证，默认关闭（D6）。
- P6-01b 通过（返工 1 次）：菲伦 L1、月城柳 L1（角色 LoRA 由训练 tag 元数据匹配）、芙莉莲 L2、约尔 L2（无参考测试图 + 筛选后的 Danbooru 参考比对）。层级判定的可靠依据只有发色，其余外貌项标注未经可靠审核。
- P5-02 部分通过：修复循环（诊断→假设→改动→结果、预算、无改善停止、相邻复核、指令编辑优先）接入正式 CLI。约尔组只看图已能读出完整故事（监工判断），但执行 45.07 分钟超限 4 秒、最终盲审超时；芙莉莲组叙事仍含糊。
- **P5 阶段门槛：部分通过**（监工判定）。
- P7-01 通过：统一入口（make/approve/status/resume/deliver，JSON 输出、needs_user 提问）、SKILL.md、安装脚本。监工已安装到 `~/.hermes/skills/story-cg-v1/`。
- **Hermes 实测 T1（07:30–07:46，先看分镜模式，月城柳/雨中送猫）**：Hermes 按 SKILL.md 正确调用 `make --storyboard-first` 并每 25–100 秒轮询 `status`（会话 `20261001_073053_2c1f4e`，转录存 `verify/hermes/t1-session-transcript.json`）。但分镜阶段被 P5-02 新增的"物体脱离"规则误报（把"小猫放到门口"当成"飞走"）连续拒绝，10 次 LLM 调用约 14 分钟后停止；监工给 Hermes 的 15 分钟超时先到，Hermes 未来得及回复。结论：Hermes 调用链路正确，分镜规则有缺陷。派 P7-02 修复。
- 下一步：P7-02 修好后重做 Hermes 实测（先看分镜 → 确认出图；默认模式；中断恢复）。

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
- D5 身份控制：IPAdapter plus-face SDXL，权重 0.4，参考=定妆图面部裁切（P3-01 试点，证据 `review/P3-01-pilot.jpg`）。0.7 否决（画风崩坏）。
- D6 修复手段：区域重绘默认关闭（视觉定位不可靠，P5-01/P5-01b 未验证成立）；新增 Qwen 指令编辑作为道具数量/状态、瞳色、配饰的首选修复；重布局用于构图/动作；面部精修用于小脸身份细节。
- D7 定妆门禁（P5-02 执行者提问，监工决定）：来源期望为空（expected=[]）的非必需项被判 not_visible 时，不阻塞定妆；门禁要求所有有来源依据的必需项通过。原审核结论原样保留，非必需不可见项在结果中标注"未经审核"。理由：与 P4-02 规则"必需项不可见才阻塞"一致，不是放宽对已检出错误的判定。
- D2 Codex 调用：`codex exec -C <workdir> --sandbox workspace-write -c sandbox_workspace_write.network_access=true --json -`；返工 `codex exec resume <id> -c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=true --json -`。

## 工单列表

| 工单 | 结论 | 返工次数 | 会话 ID | 证据 |
|---|---|---|---|---|
| P0-01 冒烟 | 通过 | 0 | 01a0f225-82be-7381-91df-8a7802b4eb8e | `smoke/*.json`、`reports/P0-01.md`；监工复核 JSON 合法、vlm_chat 内容为 pong |
| P7-02 Hermes 实测问题修复 | 通过 | 0 | 01a0f4bb-deca-79e1-8e44-fd627e54136e | `reports/P7-02.md` |
| P7-01 入口命令+SKILL.md+安装包 | 通过（默认 pytest 不可移植，待修） | 0 | 01a0f494-6d39-7361-b928-2dbcfef41136 | `skill/story-cg-v1/SKILL.md`、`reports/P7-01.md` |
| P5-02 自动修复循环+两组实跑 | 部分通过（约尔超时 4 秒、盲审超时；芙莉莲叙事未读出） | 2 | 01a0f42b-5b48-7372-a3e5-62976ad718fa | `review/P5-02-约尔-前后.jpg`、`review/P5-02-芙莉莲-前后.jpg` |
| P6-01b L1/L2 层级实测 | 通过 | 1 | 01a0f401-136d-7741-b331-6331572e7a3d | `review/P6-01b-*.jpg`、`runs/p6_01b/*/identity.json` |
| P5-01b 区域重绘返工+第 5 例 | 部分通过 | 0 | 01a0f3e7-be0b-7ed3-ba6d-b214257f71d2 | `review/P5-01b-repairs.jpg` |
| P6-01a 名字消歧+原设补全 | 通过 | 1 | 01a0f3b4-e778-77b1-b1b4-a717c595fbae | `runs/p6_01a_resolve/`、`reports/P6-01a.md` |
| P5-01 修复原语 | 部分通过（区域重绘不成立；重布局被外部宕机阻塞） | 0 | 01a0f38f-4a7b-7ae0-8d74-3ccfc0eefda4 | `review/P5-01-repairs.jpg`、`verify/p5-01_face1.jpg` |
| P4-02 审核器修正+冻结+校准 | 通过 | 0 | 01a0f35f-69e8-7512-8405-30d92f31ebb4 | `calibration/summary.md`、`config/review_reliability.json`、`verify/p4_recompute.py` |
| P4-01 校准样本 | 通过 | 2（均为监工工单澄清） | 01a0f349-3d14-7830-9295-e30725058038 | `review/P4-samples.jpg`、`verify/P4-labels.json`（监工逐项真值） |
| P3-02 整组有/无参考对照 | 通过（P3 部分达成） | 0 | 01a0f320-0b87-78a1-8d31-50f8de49f355 | `review/P3-02-芙莉莲-对照.jpg`、`review/P3-02-约尔-对照.jpg` |
| P3-01 定妆+参考机制试点 | 通过 | 0 | 01a0f2f4-1b7f-70a1-9a68-3570d3c53297 | `review/P3-01-pilot.jpg`、`runs/p3_01_pilot_20260930/manifest.json` |
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
| P3-01 | 17 | 31 | 50 |
| P3-02 | 25 | 65 | 75 |
| P4-01 | 16 | 0 | 91 |
| P4-02 | 0 | 126 | 91 |
| P5-01 | 5 | 38 | 96 |
| P6-01a | 0 | 0 | 96 |
| P5-01b | 7 | 36 | 103 |
| P6-01b | 4 | 24 | 107 |
| P5-02 | 30 | 142（+LLM 27） | 137 |
| P7-01 | 0 | 0 | 137 |
| P7-02 | 0 | 0（LLM 4） | 137 |

## 审核器校准结果（P4-02，监工独立复算确认）

先按 P1–P3 观察到的漏洞做通用修正（不可见项 not_visible、道具逐件清点、景别由脚本判定、bbox 像素坐标换算），冻结后在监工确认的 21 个样本上各跑 2 次（126 次调用）。

| 审核项 | error 样本/观测 | 判 fail 检出率 | 其余 | ok 误报率 | 结论 |
|---|---|---|---|---|---|
| 发色 | 3/6 | 100% | — | 0% | **可靠** |
| 服装 | 3/6 | 100% | — | 0% | **可靠** |
| 道具（数量/持有/状态） | 9/18 | 89% | 2 次判不可见 | 0% | **可靠** |
| 动作 | 7/14 | 100% | — | 0% | **可靠** |
| 瞳色 | 5/10 | 40% | 4 次只判 minor，2 次不可见 | 0% | 不可靠 |
| 配饰/发饰 | 3/6 | 33% | 4 次判不可见；正确样本 26/30 次也判不可见 | 0% | 不可靠 |
| 整体角色（聚合） | 9/18 | 44–67%（口径不同） | 其余判不可见 | 0% | 不可靠 |
| 发型 | 2/4 | 50% | 2 次放过 | 0% | 未验证（样本不足） |

两次运行结论不一致率 0/21。运行时审核结果中不可靠/未验证项自动标注"该项未经可靠审核"。**瞳色与配饰是身份的关键项，却不可靠**——交付说明里不能凭审核器声称这两项已验证，需要监工/用户看图。

## 审核器与监工判断不一致记录

| 来源 | 审核器结论 | 监工看图结论 | 说明 |
|---|---|---|---|
| P1-03 对 P1-02 组 | 整组未通过；盲审未读出"拿错伞" | 一致 | 叙事层判对 |
| P1-03 对 P1-02 组 | 场景连续性 4 镜全 pass | 不一致：列车颜色/车型每镜都变 | 场景连续性漏判；同一次调用在盲审里自己描述了列车变化 |
| P1-03 对 P1-02 组 | 第 3 镜 hands pass、prop_contact minor | 不一致：头顶开伞无可见握持 | 手-道具接触漏判 |
| P1-03 对 P1-02 组 | 第 1/2/4 镜构图"符合 medium" | 不一致：实际近全身 | 景别判断不可靠 |
| P3-02 芙莉莲无参考 | 第 4 镜 eye_color fail | 不一致：监工看是绿瞳 | 瞳色误报 |
| P3-02 芙莉莲 IPAdapter/混合 | 第 1 镜远景 eye_color pass | 不一致：远景眼睛偏黄/金、细节不足 | 小脸瞳色不可观察却判 pass |
| P3-02 芙莉莲混合 | 第 3 镜 action/prop_contact pass | 不一致：两把开伞 + 多出一把闭伞 | 道具数量错误漏判（第三次） |
| P3-01 试点 | 上半身近景 brown boots pass | 不一致：画面里根本没有靴子 | 不可观察项未判 fail（违反自身规则） |
| P3-01 试点 | M1 0.7 配饰 pass | 不一致：耳饰由细长垂饰变成大圆红宝石 | 小配饰形状变化漏判 |
| P3-01 试点 | 基线第 3 镜 eye_color fail | 一致：蓝瞳 | 瞳色大偏差能检出 |
| P1-04 组 | 第 4 镜 action/prop_contact pass | 不一致：两把伞都在手里，没有放下红伞 | 持有关系漏判（第二次出现） |
| P1-03 对 P1-02 组 | 身份/服装多数 pass | 基本一致（参考是第 1 镜，不是定妆图） | 仅能说明组内相对一致 |
| P5-02 约尔组 | 整组未通过（最终盲审超时） | 不一致：监工只看图能读出"戴帽走路→帽子被吹飞→追→接住"完整故事 | 超时按规则判未通过，不是读不懂 |
| P5-02 芙莉莲组 | 盲审未读出拿错伞 | 一致 | 归属类剧情仍不可读 |
| P6-01b 芙莉莲（首次） | 服装多项 fail → 层级 pending | 不一致：测试图明确是芙莉莲 | 参考选到 chibi/表情包/cosplay 且比对方向误读；返工后判 L2 |

## 阻塞项

- ~~**ComfyUI 主机宕机**~~ 已恢复（03:06，约 20 分钟）：2026-10-01 约 02:30 起 `127.0.0.1`（经 VPN tun0）ping 100% 丢包、8188 端口超时；视觉服务 `127.0.0.1` 正常。不在我控制范围内（远程 Windows 机器，可能休眠或 VPN 断开）。已开后台轮询，恢复后继续需要出图的工单。期间先做不依赖出图的部分。

## 待决

1. TASK.md 中的【】占位未填写，已按上表取保守值继续，请确认（尤其是节点/模型下载选择了 A 禁止）。
2. **是否允许安装一个面部/物体检测模型**（即改选 B）：ComfyUI 上没有任何检测模型，区域重绘只能靠视觉大模型给框，而它定位物体不可靠（P5-01/P5-01b），所以局部重绘被默认关闭。若允许装一个 anime 面部/物体检测模型，局部修复（去掉多余物体、修手）可以重新验证。当前保守方案：不装，用 Qwen 指令编辑和重布局代替。
3. **瞳色/配饰审核不可靠**：要么接受"需人工看图确认"，要么投入一个专门方法（例如按面部裁切做颜色采样比对）。当前：交付说明标注"未经可靠审核"。
4. **每组 45 分钟上限偏紧**：4 镜 + 自动修复 + 三层审核实测 42–45 分钟（约尔组超 4 秒）。可考虑放宽到 60 分钟，或减少每轮审核调用。当前：保持 45 分钟，超限如实报告。
5. **盲审超时**：4 张全尺寸图一次发给视觉模型偶尔超过 180 秒。可考虑缩图后盲审或放宽超时；当前按规则判未通过。
