# 工单 CAP-04：排除提示词干扰后补测 10 格（20 张）

先读 `TASK-CAP.md` §2.2、AGENTS.md 补充禁令第 6、7 条，以及 `reports/CAP-02.md` 和 `reports/CAP-03.md`。

## 背景

用户看过首轮结果后指出，有些格子的失败可能是提示词写坏了，不是模型画不出来：
- 场景块每镜塞了 5–6 个物件，会把近景、特写拉远（第 17 项）；
- 第 6 项场景里有 computer keyboard，两个角色都画成了在打字。

本单按用户给的规则重写这些格子的场景，再补测一次：近景场景 tag 不超过 2 个，去掉会引出其他动作的物件。

监工另外核对过：40 份请求的负面提示词里都没有 `multiple girls`、`2girls`。本单统一补上这两个，再加 `multiple views`，原因是月城柳的重复主角多是两三个人并排、姿势相同，像角色设定图。

## 做法

**底图**：以 `probe/cap02_plan.json` 里该格 `s1`、`s2` 两条的 `payload` 为底，也就是 CAP-02 实际提交的工作流。不要用 `probe/prompts.json`：第 17 项的景别在那里还是改动前的版本。

**只改三处，其他节点、参数、seed、LoRA、参考图一律不变：**

1. **正向提示词**：把下表"原文"那一段替换成"改写"。每条原文在该格 s1、s2 的正向提示词里都只出现一次，监工已核对过。替换后先断言新文本里不含原文。
2. **负向提示词**：在末尾追加 `, multiple girls, 2girls, multiple views`。
3. **filename_prefix**：改成 `probe/CAP-04/item{NN}/{L1|L2}/r{1|2}_{seed}`，不再沿用旧前缀。

**seed**：r1 用该格 s1 的 seed，r2 用 s2 的 seed（成对对照，同一个 seed 前后各一张）。下表的 seed 是监工从 `probe/runs.jsonl` 抄来的，以文件为准。

| 格 | 原文 → 改写 | r1 seed / r2 seed |
|---|---|---|
| 01 L1 | `solo, outdoors, park, tree, grass, path, bench, building, day, wide shot` → `solo, outdoors, park, tree, path, day, wide shot` | 2486996726 / 1041775343 |
| 02 L1 | `solo, outdoors, street, sidewalk, road, building, tree, lamppost, day, wide shot` → `solo, outdoors, street, sidewalk, building, day, wide shot` | 2387087358 / 1689918833 |
| 03 L1 | 同 02 | 3948542918 / 1634065240 |
| 05 L1 | `solo, indoors, bedroom, bed, rug, wooden floor, window, curtains, day, cowboy shot` → `solo, indoors, bedroom, wooden floor, window, day, cowboy shot` | 262689418 / 360479082 |
| 06 L1 | `solo, indoors, office, desk, chair, computer, monitor, computer keyboard, bookshelf, window, day, cowboy shot` → `solo, indoors, office, desk, chair, day, cowboy shot` | 3154249835 / 3098694889 |
| 06 L2 | 同 06 L1 | 240001466 / 3333031000 |
| 13 L2 | `solo, indoors, living room, couch, table, window, bookshelf, potted plant, day, upper body, close-up` → `solo, indoors, living room, upper body, close-up` | 2972674097 / 1199104410 |
| 17 L1 | `solo, indoors, living room, couch, table, window, bookshelf, potted plant, day, close-up, portrait` → `solo, indoors, living room, close-up, portrait` | 584822560 / 1833542175 |
| 17 L2 | 同 17 L1 | 2502925501 / 2398531460 |
| 19 L1 | `solo, indoors, bedroom, window, curtains, lamp, bed, city lights, night, cowboy shot` → `solo, indoors, bedroom, window, city lights, night, cowboy shot` | 1319666128 / 3363681573 |

## 提交规则

和 CAP-02、CAP-03 相同：

- 每次提交前查 `/queue`，串行提交，一次一张；
- 只出一次，不修复、不重试、不挑图。出图失败就如实记为失败，不补出；
- `/history` 核对**按 prompt_id 逐条**调 `/history/<prompt_id>`，不要拉完整的 `/history`。到 Windows 主机的链路丢包约 33%，完整响应会被截断；
- 传输失败可以退避后重试**查询**，但不得重复**提交**。提交前先写意图记录，提交后写回执，和 CAP-03 一样。

## 产出

- 记录写进新文件 `probe/retest_runs.jsonl`，不要追加到 `probe/runs.jsonl`。每条带：
  - `"order": "CAP-04"`、`"paired_with": "<原 run_key>"`；
  - 改写前后的正向、负向提示词全文；
  - seed、prompt_id、sha256、耗时。
- 原图存为 `probe/images/<项号>_<角色名>_r<1|2>.png`。
- 对照拼图 `probe/sheets/retest_NN_<角色名>.jpg`，每格一张，分两行：
  - 上排：原提示词的 s1、s2；
  - 下排：改写后的 r1、r2；
  - 标出 seed 和"原 / 改写"。
- 报告 `reports/CAP-04.md`，写清：
  - 用量；
  - 逐条 `/history` 核对结果；
  - 每张图实际提交的正向、负向提示词与本单要求逐字一致的证据。

## 不许做

- 改动上表以外的提示词内容（包括身份、服装、镜头块和画质词）；
- 改 seed、修复、重试、挑图；
- 读 `verify/` 下的任何文件；
- 改已有代码和配置、已有的 `probe/` 记录（`runs.jsonl`、`reviewer.jsonl` 等）；
- git commit。

## 预算

出图 **20 张**，本阶段累计到 120/120，额度用完。视觉调用 0 次。时长 45 分钟。
