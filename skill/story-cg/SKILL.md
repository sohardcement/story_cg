---
name: story-cg
description: 把一个具名单主角和一段剧情做成 4–6 镜连续剧情 CG。先给用户看分镜和定妆图，确认后每镜出候选，用户逐镜看图选择，交付拼图和结果说明。
---

单主角剧情 CG（单人日常，道具只作陪衬）。不支持两个具名角色同框，道具归属、状态和远处小人物不擅长，剧情依赖道具时照常出图并提醒用户。入口是本 skill 目录下的 `scripts/storycg`，用绝对路径在终端执行。默认流程由用户逐镜看图选择，不调用视觉模型。

- 角色：只支持 WAI 清单里的角色。清单外时 `compile`、`make` 返回 `needs_user`，`reason` 为 `character_not_in_wai_list`。原样转告用户，不重试、不换 seed。

## 标准流程

1. `scripts/storycg beats-guide --character '角色名' --shots 4`：读例子、规则、标准服装 tag、词汇表。最后一节「分镜文件写到这里」给出这次的分镜文件路径，每次运行都是新建的空目录。
2. 把 beats 文件（一个 JSON 对象）写到这个路径，不用以前的分镜文件，不自己另起文件名。`scripts/storycg compile --character '角色名' --beats '<路径>' --shots 4`：只编译不出图。invalid 时按镜号和字段改（写回同一路径），直到 valid；看 rewrites 和 prompt_preview（每镜最终提示词）。
3. `scripts/storycg make --character '角色名' --story '剧情' --beats '<路径>' --shots 4`：定妆图生成后停在 `awaiting_approval`。每 15–30 秒 `scripts/storycg status '<job_dir>'` 轮询。`make` 加 `--no-confirm` 可跳过这次确认，每镜仍会等用户选。
4. `awaiting_approval`：给用户看 `storyboard`、`confirmation` 里的 `portrait_image`，转述 `story_note`、`compile_rewrites`。等用户确认。改分镜：`approve '<job_dir>' --beats '<新文件>'`；定妆图不满意：`approve '<job_dir>' --redo-portrait`。
5. 确认：`scripts/storycg approve '<job_dir>'`。每镜出完停在 `awaiting_pick`：给用户看 `pending_pick` 里的 `sheet` 和候选编号，问选哪张。
6. 用户选定：`scripts/storycg pick '<job_dir>' --shot N --candidate K`，任务自动出下一镜。要再出一批：`pick '<job_dir>' --shot N --more`。全部选完到 `completed`。用户说了不在、不用问他选图时，你自己看 `sheet` 选，`pick` 加 `--by agent`（结果说明会写成由调用方看图选择），最后告诉用户每镜是你选的、为什么选。
7. 交付后 `status` 的 `artifacts.collage` 是拼图，`artifacts.candidate_sheet` 是候选总图，`artifacts.images` 是每镜单图；看图、读 result.md，把拼图路径（`artifacts.collage`）给用户。事后换某镜：同样 `pick`，只重建拼图和说明。
8. 某一镜的构图不满意：`reshoot '<job_dir>' --shot N --framing medium`（medium 是 cowboy shot，也可 --angle），这一镜重出一批，等用户选，其他镜的选择不变。

## 其他命令

- `cancel '<job_dir>'`：停止任务的唯一方式，不要自己 kill 进程或动 ComfyUI 队列；之后可 `resume`。
- `resume`：中断或资源恢复后用，再轮询 status。`deliver`：只重建交付文件。
- `prune --days 7`：只列出可清理的中间图；用户要求时才加 `--apply`。
- 只在 `needs_user` 为 true 时提问，按 `question` 和 `options` 问用户。身份不明确时：`resolve '<job_dir>' --tag '候选 tag'` 或 `--work '作品名'`，然后 resume。
- `make` 和 `status` 输出里 `host_warning` 不为空时（ComfyUI 主机慢、预计超时），马上原样告诉用户，任务照常继续，不要自己 cancel。
- stdout 是 JSON，stderr 是进度。`stopped` 且没有交付物时不宣称成功，有 `error` 时原样说明；`interrupted` 时 resume。

不改项目代码、配置或任务状态文件；遇到 `error`、`reason`、`question` 原样转告，不自行修代码或改状态来绕过。
