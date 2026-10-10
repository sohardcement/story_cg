---
name: story-cg
description: 把一个具名单主角和一段剧情做成 4–6 镜连续剧情 CG。先给用户看分镜和定妆图，确认后每镜出候选并自动挑选，交付拼图和结果说明；用户可以换某镜的候选。
---

使用本 skill 处理单主角剧情 CG（单人日常，道具只作陪衬）。擅长表情、动作、身份与服装一致，以及场景变化；不擅长道具的数量、归属和状态，不支持两个具名角色同框，也不擅长“拿错东西”这类靠道具归属推理的情节，以及远处的小人物。道具问题照常记录为已知问题，不否决、不修复；道具依赖剧情照常出图并提醒可能读不出，不额外提问。以本 skill 所在目录定位 `scripts/storycg`，用其绝对路径在终端执行；流程由 Python 编排。

- 标准流程（beats 方式）：
  1. 运行 `scripts/storycg beats-guide --character '角色名' --shots 4`，读完输出的 schema、写作规则、该角色的标准服装 tag 和本层级的词汇表（含撞键说明）。剧情没换衣服时服装 tag 原样用标准服装；换衣服的镜头写新衣服的 tag；
  2. 按说明写好 beats 文件（一个 JSON 对象，保存为文件）；
  3. 运行 `scripts/storycg compile --character '角色名' --beats '<beats 文件>' --shots 4` 自查。它只解析身份、判定层级、编译分镜，不出镜头图。`invalid` 时按 `errors` 里的镜号和字段修改 beats 文件，再编译，直到 `status` 为 `valid`；
  4. 运行 `scripts/storycg make --character '角色名' --story '剧情' --beats '<beats 文件>' --shots 4`。beats 方式默认在分镜编好、定妆图生成好以后停在 `awaiting_approval`，不出镜头图。轮询 `scripts/storycg status '<job_dir>'`（约 15–30 秒一次）；
  5. 到 `awaiting_approval` 时，把 `storyboard`（每镜摘要）和 `confirmation`（层级、定妆图 `portrait_image`／`portrait_face`、每镜候选张数）展示给用户；摘要里有每镜天气和场景变化，`confirmation` 里有 `story_note`（分镜改写提醒）、`compile_rewrites`（编译时的自动改写）、`portrait_review`（定妆图原始审核结论；未通过时列出未通过的项和原因，审核只记录、不拦截），这些要一并转述给用户，等用户确认；
  6. 用户确认：`scripts/storycg approve '<job_dir>'`，之后轮询 status 直到 `completed`。用户要改分镜：写新的 beats 文件，运行 `scripts/storycg approve '<job_dir>' --beats '<新 beats 文件>'`，仍停在 `awaiting_approval`，展示新的摘要（定妆图不重出）。用户不满意定妆图：`scripts/storycg approve '<job_dir>' --redo-portrait`，出一张新定妆图，仍停在 `awaiting_approval`，再次展示；
  7. 不想确认、一路跑到交付：`make` 加 `--no-confirm`；
  8. 交付后，把拼图和候选总图（`candidate_sheet.jpg`）给用户看，读取 `result.md`；用户要换某镜的图，运行 `scripts/storycg pick '<job_dir>' --shot N --candidate K`，只换选择、重建拼图和 `result.md`，不出图、不重审。K 可以是被筛掉的候选，此时结果会注明；换完以后把新拼图再给用户。
- 中断或资源恢复：`scripts/storycg resume '<job_dir>'`，再轮询 status；未确认分镜不会自动出图。
- 重建交付文件：`scripts/storycg deliver '<job_dir>'`，不出图、不审核。

stdout 是 JSON，stderr 是进度／诊断。`progress.completed_shots` 是已挑选的镜数（不是已出图的镜数）；`progress.shots` 给出每镜已出图／应出、已审核数和是否已挑选，`final_review_done` 表示终审是否完成。仅当 `needs_user=true` 时按 `question` 和 `options` 提问：角色身份不明确、关键资源不可用、要求互相冲突。身份选择可用底层 `scripts/storycg resolve '<job_dir>' --tag '候选tag'` 或 `--work '作品名'`，再 resume；冲突修改后重新 make。`awaiting_approval` 是用户主动选择的确认步骤。普通创作选择自行决定；多具名角色转述 `notices`，按指定单主角继续。

`completed` 或 `stopped` 时展示已有交付：按当前环境展示图片。能直接看图的环境（Claude Code、Cursor 等）先用读图工具打开编号拼图和候选总图，再把图片的绝对路径告诉用户；Hermes 环境用 `MEDIA:<artifacts.preview 的绝对路径>` 展示编号拼图。result.md 里的“已观察到的偏差”一节（通过了筛选／审核观察到的偏差／未经可靠审核，以及整组原始审核结论）也要转述给用户。随后附简短结果、已知问题及 `artifacts.images/result/manifest` 路径。读取 result.md：它按概况、文件、每镜（选中、理由、被筛掉的候选）、看图复述、已知问题排列，逐项审核细节见 review.md；result.md 里写明“该项未经可靠审核”的，要如实告诉用户，尤其配饰、发型、整体角色，请用户看图确认。出错时说明 error／repair_blocked，已有产物不代表审核通过。状态为 interrupted 时 resume；无交付物的 stopped 不宣称成功。

运行时代理仅通过上述 CLI 操作任务，不得直接修改项目代码、配置或任务状态文件。遇到错误或问题，将 `status` 返回的 `error`／`repair_blocked`／`question` 原样告诉用户；资源恢复后可以执行 `resume` 并继续轮询。不得自行修代码或改写状态来绕过错误。

## 运行环境

- 代码：项目根目录下的 `src/storycg`。安装后 `scripts/storycg` 从 `project_path` 文件读取项目根目录（由 `tools/install_hermes_skill.sh` 写入）；没有该文件时，退回到仓库目录结构，即 skill 目录往上两级为项目根目录，且根目录下需要有 `src/storycg/__main__.py` 和 `.venv/bin/python`，否则输出 `needs_user` 的 JSON 并以非零退出。
- 配置：项目根目录下的 `config/default.json`，其中 `comfyui_url`（ComfyUI 地址）和 `model_service_url`（视觉模型服务地址）指向局域网内的两台机器。`make` 等命令可用 `--config <路径>` 换用其他配置文件，默认即 `config/default.json`。
- 输出：每次 `make` 在项目根目录的 `jobs/cg-<12 位随机串>/` 下新建任务目录，状态、分镜、候选、拼图（`artifacts.preview`）、候选总图、`result.md` 和 `manifest` 都在其中；路径以 `status` 返回的 `artifacts` 为准。
- 依赖：项目根目录下的 `.venv`（Python），由 `scripts/storycg` 直接调用，不需要另行激活。
