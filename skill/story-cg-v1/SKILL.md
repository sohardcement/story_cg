---
name: story-cg-v1
description: 根据一个具名角色和剧情生成 4–6 张连续 CG；支持先看分镜、确认修改、查看进度、恢复中断与重新交付。
---

使用本 skill 处理单主角剧情 CG。v1 擅长表情、动作、身份与服装一致及场景变化；不擅长道具归属/状态/数量、两个具名角色同框、“拿错东西”类情节和远处小人物。道具问题照常记录为已知问题，不否决、不修复；道具依赖剧情照常出图并提醒可能读不出，不额外提问。以本 skill 所在目录定位 `scripts/storycg`，用其绝对路径在终端执行；项目路径由安装脚本配置，流程由 Python 编排。

- 标准流程（V2-24，TASK-v2 §10.1）：
  1. 运行 `scripts/storycg beats-guide --character '角色名' --shots 4`，读完输出的 schema、写作规则、该角色的标准服装 tag 和本层级的词汇表（含撞键说明）。剧情没换衣服时服装 tag 原样用标准服装；换衣服的镜头写新衣服的 tag；
  2. 按说明写好 beats 文件（一个 JSON 对象，保存为文件）；
  3. 运行 `scripts/storycg compile --character '角色名' --beats '<beats 文件>' --shots 4` 自查。它只解析身份、判定层级、编译分镜，不出镜头图。`invalid` 时按 `errors` 里的镜号和字段修改 beats 文件，再编译，直到 `status` 为 `valid`；
  4. 运行 `scripts/storycg make --character '角色名' --story '剧情' --beats '<beats 文件>' --shots 4`。beats 方式默认在分镜编好、定妆图生成好以后停在 `awaiting_approval`，不出镜头图。轮询 `scripts/storycg status '<job_dir>'`（约 15–30 秒一次）；
  5. 到 `awaiting_approval` 时，把 `storyboard`（每镜摘要）和 `confirmation`（层级、定妆图 `portrait_image`／`portrait_face`、每镜候选张数）展示给用户，等用户确认；
  6. 用户确认：`scripts/storycg approve '<job_dir>'`，之后轮询 status 直到 `completed`。用户要改分镜：写新的 beats 文件，运行 `scripts/storycg approve '<job_dir>' --beats '<新 beats 文件>'`，仍停在 `awaiting_approval`，展示新的摘要（定妆图不重出）。用户不满意定妆图：`scripts/storycg approve '<job_dir>' --redo-portrait`，出一张新定妆图，仍停在 `awaiting_approval`，再次展示。beats 方式不支持 `--edit`；
  7. 不想确认、一路跑到交付：`make` 加 `--no-confirm`；
  8. 交付后，把拼图和候选总图（`candidate_sheet.jpg`）给用户看，读取 `result.md`；用户要换某镜的图，运行 `scripts/storycg pick '<job_dir>' --shot N --candidate K`，只换选择、重建拼图和 `result.md`，不出图、不重审。K 可以是被筛掉的候选，此时结果会注明；换完以后把新拼图再给用户。
- 旧流程（不加 `--beats`）：`scripts/storycg make --character '角色名' --story '剧情' [--storyboard-first]`，分镜由流水线调用本地模型编写；`approve --edit '修改说明'` 只用于这种方式。
- 中断或资源恢复：`scripts/storycg resume '<job_dir>'`，再轮询 status；未确认分镜不会自动出图。
- 重建交付文件：`scripts/storycg deliver '<job_dir>'`，不出图、不审核。

stdout 是 JSON，stderr 是进度／诊断。仅当 `needs_user=true` 时按 `question` 和 `options` 提问：角色身份不明确、关键资源不可用、要求互相冲突。身份选择可用底层 `scripts/storycg resolve '<job_dir>' --tag '候选tag'` 或 `--work '作品名'`，再 resume；冲突修改后重新 make。`awaiting_approval` 是用户主动选择的确认步骤。普通创作选择自行决定；多具名角色转述 `notices`，按指定单主角继续。

`completed` 或 `stopped` 时展示已有交付：回复中用 Hermes 媒体标记 `MEDIA:<artifacts.preview 的绝对路径>` 展示编号拼图，附简短结果、已知问题及 `artifacts.images/result/manifest` 路径。读取 result.md：候选方式下它按概况、文件、每镜（选中、理由、被筛掉的候选）、看图复述、已知问题排列，逐项审核细节见 review.md；修图方式的 result.md 仍按旧格式，要如实保留“该项未经可靠审核”，尤其配饰、发型、整体角色，请用户看图确认。出错时说明 error／repair_blocked，已有产物不代表审核通过。状态为 interrupted 时 resume；无交付物的 stopped 不宣称成功。

运行时代理仅通过上述 CLI 操作任务，不得直接修改项目代码、配置或任务状态文件。遇到错误或问题，将 `status` 返回的 `error`／`repair_blocked`／`question` 原样告诉用户；资源恢复后可以执行 `resume` 并继续轮询。不得自行修代码或改写状态来绕过错误。
