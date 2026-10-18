# 当前默认（`--profile default`）

改任何默认（配置、词表、写作说明）时同步改这张表。`tests/test_defaults_doc.py` 会读 `load_profile('default')`，逐行核对第一张表的「配置键」和「值」（值按 JSON 比较）；第二张表核对代码里的词表；对不上测试就失败。决定的来龙去脉在 TASK-v2.md 对应条目里。

## 配置里的值

| 项 | 配置键 | 值 | 决定 |
|---|---|---|---|
| 底模 | `checkpoint` | `"waiIllustriousSDXL_v170.safetensors"` | — |
| 风格 LoRA | `style_loras.0.name` | `"akml_style_v3.safetensors"` | D95 |
| 风格 LoRA 模型强度 | `style_loras.0.strength_model` | `0.7` | D112 |
| 风格 LoRA 文本强度 | `style_loras.0.strength_clip` | `0.7` | D112 |
| 正向前缀 | `positive_prefix` | `"akml, masterpiece, best quality, general, safe, sfw"` | D95 |
| 脸部 IPAdapter | `identity_control.mechanism` | `"none"` | D109（0.4 / 0.3 的数值留给 `--identity-control ipadapter`） |
| 采样步数 | `sampler.steps` | `28` | — |
| CFG | `sampler.cfg` | `5.5` | — |
| 采样器 | `sampler.sampler_name` | `"euler_ancestral"` | — |
| 调度 | `sampler.scheduler` | `"normal"` | — |
| 画布 medium | `resolutions.medium` | `[1024, 1024]` | — |
| 画布 closeup | `resolutions.closeup` | `[1024, 1024]` | — |
| 画布 full | `resolutions.full` | `[832, 1216]` | — |
| 画布 wide | `resolutions.wide` | `[1216, 832]` | — |
| medium 正向景别 tag | `framing.medium.tags` | `["cowboy_shot"]` | D104；D115 试过加 upper body，没过 |
| medium 负向景别 tag | `framing_negative_tags.medium` | `["wide_shot"]` | D115 试过加 full body，没过 |
| front 角度 tag | `camera_tags.front` | `[]` | 空：front 镜头不加角度 tag（D114 分开重判：front 有 9 格画成 side） |
| 去鞋袜腿部 tag（closeup、medium） | `drop_leg_tags` | `true` | D116 |
| 面部精修阈值（脸长边像素） | `face_refine.max_face_px` | `140` | V2-50 |
| 面部精修 denoise | `face_refine.denoise` | `0.5` | V2-50 |
| 每镜候选数 | `candidates_per_shot` | `3` | D61 |
| 选图方式 | `review_mode` | `"human"` | D101；无人值守时调用方看图选、`pick --by agent`（loop B） |
| 每个任务出图上限 | `budget.max_images_per_job` | `60` | — |
| 写分镜的模型 | `llm.model` | `"Qwen3.8-Flash-Next-Uncensored-MLX-Serve-4bit"` | D113 |
| 写分镜温度 | `llm.temperature` | `0.7` | D113 |
| 写分镜思考模式 | `llm.enable_thinking` | `false` | D113 |

hires fix 不是配置项：D111 试过，不如不开，没有接进流水线。

## 代码里的词表

| 项 | 来源 | 值 | 决定 |
|---|---|---|---|
| action_tag 枚举 | `beats.measured_action_tags()` | `["stretching", "drinking", "waving", "reading", "writing", "cooking", "eating", "yawning", "splashing", "slashing", "attack", "inhaling", "tasting"]` | D113-d、D117 |
| pose 黑名单 | `beats_v3.POSE_BLOCK` | `["animal_hug", "curtain_grab", "holding_phone", "holding_umbrella", "on_back", "on_stomach", "pillow_hug", "playing_guitar", "pouring_onto_self", "under_covers"]` | D113-e |
| 互斥场景 tag | `beats_v3.EXCLUSIVE_TAGS` | `[["indoors", "outdoors"], ["day", "night"], ["sunny", "rain"], ["indoors", "veranda"], ["indoors", "engawa"]]` | D113-c、D113-e |
| 保留服装名 | `beats_v3.RESERVED_OUTFIT_NAMES` | `["standard", "default"]` | loop A 第 1 轮 |
