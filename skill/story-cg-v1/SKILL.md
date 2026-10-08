---
name: story-cg-v1
description: 根据一个具名角色和剧情生成 4–6 张连续 CG；支持先看分镜、确认修改、查看进度、恢复中断与重新交付。
---

使用本 skill 处理单主角剧情 CG。v1 擅长表情、动作、身份与服装一致及场景变化；不擅长道具归属/状态/数量、两个具名角色同框、“拿错东西”类情节和远处小人物。道具问题照常记录为已知问题，不否决、不修复；道具依赖剧情照常出图并提醒可能读不出，不额外提问。以本 skill 所在目录定位 `scripts/storycg`，用其绝对路径在终端执行；项目路径由安装脚本配置，流程由 Python 编排。

- 默认：`scripts/storycg make --character '角色名' --story '在山顶迎着朝阳伸懒腰，被风吹乱后有点生气，最后看着日出笑了' [--shots 4]`。
- 分镜由 agent 自己写（新流程，优先使用）：
  1. 运行 `scripts/storycg beats-guide --shots 4`，读完输出的 schema 和写作规则；
  2. 按说明写好 beats 文件（一个 JSON 对象，保存为文件）；
  3. 运行 `scripts/storycg compile --character '角色名' --beats '<beats 文件>' --shots 4` 自查。它只解析身份、判定层级、编译分镜；层级缓存未命中时层级判定可能生成 1 张探针图，其余不出图。输出 `status`。`invalid` 时 `errors` 列出每一条错误，按其中的镜号和字段修改 beats 文件，再编译，直到 `status` 为 `valid`；
  4. 运行 `scripts/storycg make --character '角色名' --story '剧情' --beats '<beats 文件>' [--shots 4]`。编译失败时任务停在 `stopped`，`error` 写出全部错误：修改 beats 文件后重新 make（或先用 compile 确认）。
  分镜由 beats 文件决定，流水线不再为分镜调用本地模型。
- 用户要求先看分镜：加 `--storyboard-first`。后台返回 `job_dir` 后轮询 `scripts/storycg status '<job_dir>'`（约 15–30 秒一次），不要阻塞等待长任务。到 `awaiting_approval` 展示 JSON 的中文 `storyboard`，等用户确认。
- 确认：`scripts/storycg approve '<job_dir>'`；修改后确认：加 `--edit '修改说明'`（也可传说明文件路径）。再轮询 status。
- 中断或资源恢复：`scripts/storycg resume '<job_dir>'`，再轮询 status；未确认分镜不会自动出图。
- 重建交付文件：`scripts/storycg deliver '<job_dir>'`，不出图、不审核。

stdout 是 JSON，stderr 是进度／诊断。仅当 `needs_user=true` 时按 `question` 和 `options` 提问：角色身份不明确、关键资源不可用、要求互相冲突。身份选择可用底层 `scripts/storycg resolve '<job_dir>' --tag '候选tag'` 或 `--work '作品名'`，再 resume；冲突修改后重新 make。`awaiting_approval` 是用户主动选择的确认步骤。普通创作选择自行决定；多具名角色转述 `notices`，按指定单主角继续。

`completed` 或 `stopped` 时展示已有交付：回复中用 Hermes 媒体标记 `MEDIA:<artifacts.preview 的绝对路径>` 展示编号拼图，附简短结果、已知问题及 `artifacts.images/result/manifest` 路径。读取 result.md；如实保留“该项未经可靠审核”，尤其配饰、发型、整体角色，请用户看图确认。出错时说明 error／repair_blocked，已有产物不代表审核通过。状态为 interrupted 时 resume；无交付物的 stopped 不宣称成功。

运行时代理仅通过上述 CLI 操作任务，不得直接修改项目代码、配置或任务状态文件。遇到错误或问题，将 `status` 返回的 `error`／`repair_blocked`／`question` 原样告诉用户；资源恢复后可以执行 `resume` 并继续轮询。不得自行修代码或改写状态来绕过错误。
