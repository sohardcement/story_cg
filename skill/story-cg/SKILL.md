---
name: story-cg
description: 把一个具名单主角和一段剧情做成 4–6 镜连续剧情 CG。先给用户看分镜和定妆图，确认后每镜出候选并自动挑选，交付拼图和结果说明；用户可以换某镜的候选。
---

单主角剧情 CG（单人日常，道具只作陪衬）。不支持两个具名角色同框。道具问题记为已知问题，不否决、不修复；道具归属、状态和远处小人物不擅长。剧情依赖道具时照常出图，提醒用户可能读不出来，不提问。入口是本 skill 目录下的 `scripts/storycg`，用绝对路径在终端执行，流程由 Python 编排。

- 角色：只支持 WAI 清单（`data/wai/waiIllustriousSDXL_v160_characters.csv`）里的角色。清单外时 `compile`、`make` 返回 `needs_user`，`reason` 为 `character_not_in_wai_list`。原样转告用户，建议换清单里的角色，不重试、不换 seed。

## 标准流程（beats）

1. `scripts/storycg beats-guide --character '角色名' --shots 4`：读例子、规则、标准服装 tag、词汇表。没换衣服用标准服装 tag，换衣服的镜写新 tag；标准服装是场合服装又不合剧情时，可以整组换成日常服装，规则见 beats-guide。
2. 按说明写 beats 文件（一个 JSON 对象）。
3. `scripts/storycg compile --character '角色名' --beats '<beats 文件>' --shots 4`：只解析和编译，不出图。返回 invalid 时按错误的镜号和字段修改，直到 valid。valid 时看 rewrites 和 prompt_preview（每镜最终提示词），确认要画的 tag 都在；要看整份分镜加 `--verbose`。
4. `scripts/storycg make --character '角色名' --story '剧情' --beats '<beats 文件>' --shots 4`：默认在分镜和定妆图生成后停在 `awaiting_approval`，不出镜头图。每 15–30 秒 `scripts/storycg status '<job_dir>'` 轮询一次。
5. `awaiting_approval`：展示 `storyboard`（每镜摘要）和 `confirmation` 里的 `portrait_image`、`portrait_face`、每镜候选数；转述 `story_note`、`compile_rewrites`、`portrait_review`（定妆图原始审核，只记录不拦截，未通过的项里哪些是不可靠项，要一并说明）。等用户确认。
6. 确认：`scripts/storycg approve '<job_dir>'`，轮询到 `completed`。改分镜：写新 beats 文件，`scripts/storycg approve '<job_dir>' --beats '<新 beats 文件>'`，仍停在确认页，展示新摘要。定妆图不满意：`scripts/storycg approve '<job_dir>' --redo-portrait`，再出一张，仍停在确认页。
7. 不想确认、一路跑到交付：`make` 加 `--no-confirm`。
8. 交付后展示拼图和候选总图，读取 result.md。换某镜：`scripts/storycg pick '<job_dir>' --shot N --candidate K`，只换选择、重建拼图和 result.md，不出图、不重审。换完把新拼图再给用户。

## 其他命令

- `cancel '<job_dir>'`：停止任务的唯一方式，不要自己 kill 进程或动 ComfyUI 队列；之后可 `resume`。
- `prune --days 7`：只列出可清理的中间图；用户要求时才加 `--apply`。
- `resume`：中断或资源恢复后用，再轮询 status。未确认的分镜不会自动出图。
- `deliver`：只重建交付文件，不出图、不审核。
- 只在 `needs_user` 为 true 时提问，按 `question` 和 `options` 问用户；创作选择自行决定。身份不明确时：`scripts/storycg resolve '<job_dir>' --tag '候选 tag'` 或 `--work '作品名'`，然后 resume。
- stdout 是 JSON，stderr 是进度；轮询时用 `progress` 告诉用户。

## 展示与转述

`completed` 或 `stopped` 时展示交付：

- 能直接看图的环境（如 Claude Code、Cursor）先用读图工具打开 `artifacts.preview` 拼图和候选总图，再给用户绝对路径；Hermes 用 `MEDIA:<artifacts.preview 绝对路径>`。
- 读 result.md：按概况、文件、每镜、已观察到的偏差、看图复述、已知问题排列，转述其中的偏差和“至少一镜未经可靠审核的类别”，请用户确认配饰、发型。
- 附结果摘要、已知问题和 `artifacts.images`、`artifacts.result`、`artifacts.manifest` 路径。
- `stopped` 且没有交付物时不宣称成功。有 `error` 时原样说明；已有产物不等于审核通过。`interrupted` 时 resume。

不改项目代码、配置或任务状态文件；要换配置让用户来改。遇到 `error`、`reason`、`question` 原样转告，不自行修代码或改状态来绕过。

## 运行环境

配置在项目根目录的 `config/default.json`，`comfyui_url`、`model_service_url` 指向局域网内的两台机器；命令可用 `--config` 换配置。每次 `make` 在项目根目录的 `jobs/` 下新建任务目录。
