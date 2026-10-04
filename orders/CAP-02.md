# 工单 CAP-02：能力摸底首轮出图（80 张，只出一次）

先读 `TASK-CAP.md`（§2.2）、AGENTS.md 补充禁令第 6、7 条，以及 `probe/prompts.md`、`reports/CAP-01.md`。

## 监工审 tag 的结论（出图前先改）

40 条请求里，下面两处要改，其余照 `probe/prompts.json` 原样出图：

1. **第 12 项**（左手拿书、右手拿杯子）：删掉 `left_hand` 和 `right_hand`。这两个 tag 在 Danbooru 上的使用数为 0，加进去只是噪声。其余 tag 不变。判图时只看"两只手各拿一样"，不考核左右。
2. **第 17 项**（特写，只拍脸）：镜头块的景别 tag 由 v1 近景映射的 `upper_body, close-up` 改成 `close-up, portrait`，其余 tag 不变。先用现有的 tag 存在性检查确认 `portrait` 存在。不存在就停下来报告，不要换别的词。

两处都是监工覆盖，不改 v1 代码。把修改前后的正向提示词、修改原因写进 `probe/overrides.json`。其余 38 条保持原样，第 11 项里重复的 `holding_cup` 也照旧，不去掉。

## 出图

- 40 条请求，每条 2 个 seed，共 **80 张**，按 `probe/prompts.json` 里的完整工作流提交，只改上面两处。
- 每次提交前查 `GET /queue`，只有队列为空才提交；一次只提交一个，等它完成再提交下一个。`filename_prefix` 以 `probe/` 开头。
- **只出一次**：不修复、不重试、不换 seed、不挑图。某张报错，记录错误并继续下一张，报错的那张计入用量，但不补出。
- 每张记录：项号、角色、seed、完整正负向、宽高、LoRA、IPAdapter 参考、ComfyUI prompt_id、执行秒数、完成时间、本地文件路径和 sha256，写入 `probe/runs.jsonl`。原图存到 `probe/images/<项号>_<角色>_s<slot>.png`。

## 判定用的拼图（给监工看）

- `probe/sheets/item_NN.jpg`：每项一张，左边月城柳两张，右边芙莉莲两张，图下标出角色、seed 和项号。不要加任何"好坏"判断。
- `probe/sheets/index.jpg`：全部 20 项的缩略总览。

## 不许做

修复、重试、换 seed；调用视觉模型；修改 `src/`、`skill/`、`config/` 等已有代码和配置；git commit；向 8188 并行提交。

## 预算

出图 **≤ 80 张**（本阶段上限 120，剩下 40 张留给后面的补图和覆盖追踪）；视觉调用 0；时长 75 分钟。

## 必须产出

`probe/overrides.json`、`probe/runs.jsonl`、`probe/images/`、`probe/sheets/`、`reports/CAP-02.md`。报告写清用量、单张耗时统计、报错清单，以及 ComfyUI `/history` 核对：每个 prompt_id 只提交过一次。
