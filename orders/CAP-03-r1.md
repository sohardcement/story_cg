# CAP-03 续跑 1：主机关机中断后续跑审核器对照

## 发生了什么（监工核实）

omarchy 主机在 2026-10-04 约 04:32 UTC（12:32 HKT）关机，15:46 HKT 前重新开机。你的会话和 `cap03.py audit` 进程都随之终止。现状：

- 补图已完成：`probe/cap03_generate_finished.json` 记录 20 张，`cap03_history_check.json` 已写。
- 审核器对照记录了 40/100 张（`probe/reviewer.jsonl` 40 行，最后一条是 `10_L2_s2`）。
- `probe/review_log.jsonl` 里有 41 个 request、40 个 response。第 41 次请求 `68ada5128de147019401c1b8fa9d8488:41`（`cap03_single_11_L1_s1`）已经发出，但没有收到响应。
- 没有 `cap03_audit_finished.json`，因为 finally 块没有机会执行。
- 监工刚确认过：ComfyUI 的 `/queue` 是空的，视觉服务 `/v1/models` 返回 200。

## 要做的事

1. **不改已有记录**：`reviewer.jsonl` 的前 40 行、`review_log.jsonl`、`cap03_audit_started.json`、`cap03_events.jsonl` 里已有的行，一律不改。
2. 在 `probe/cap03_events.jsonl` 追加一条 `request_abandoned`，写明 call_id `…:41`、purpose，原因写"主机关机，请求在途"。这次请求计入调用预算。
3. 在 `probe/cap03.py` 里加一个续跑子命令（例如 `audit-resume`），要求：
   - 只审 `reviewer.jsonl` 里还没有的 run_key，顺序和原计划一致，追加写进同一个 `reviewer.jsonl`；
   - 用新的单次标记文件（例如 `cap03_audit_resume_started.json`），有自己的 finished 文件；
   - 输入、调用的函数、记录字段都和第一段相同，另加 `"segment": 2`；
   - `11_L1_s1` 正常重审一次，记录里注明上一次请求被放弃。
4. **预算**：
   - 视觉调用全程合计不超过 110 次。已发出 41 次，所以这一段最多 69 次（剩余 60 张，留出传输重试余量）。
   - 时长：这一段从启动起重新计 60 分钟，因为关机时间不算在内。
   - 本段出图 0 张。
5. 跑完后按原工单写 `reports/CAP-03.md`。用量要分两段写；如实写明这次中断，以及续跑是怎么做的。

## 不许做

原工单的禁止项照旧：不读 `verify/cap/judgments.json`，不修复、不重试出图、不换 seed，不改已有代码和配置，不 git commit。

视觉服务持续失败时（同一张图连续传输失败 2 次，或连续 3 张失败），停下来写进报告，不要硬跑。
