# 工单 CAP-01：能力摸底的 40 条提示词（不出图）

先读 `TASK-CAP.md`（重点是 §2.1、§2.2）和 AGENTS.md（含补充禁令第 6、7 条）。

## 目标

为 TASK-CAP §2.1 的 20 个测试项、两个角色写出可以直接提交的出图请求，供监工逐项审阅。本工单**不出图**。

## 要求

1. **角色与身份**：
   - 月城柳（L1）：身份状态取 `jobs/p9_05_office/identity.json`，专用 LoRA 与强度照该任务 manifest 中的记录。
   - 芙莉莲（L2）：取 `jobs/p9_07_ownership/identity.json`。
   - IPAdapter 用 v1 现行设置（plus-face SDXL 0.4），参考图用这两个任务各自已通过门禁的定妆照（manifest 里 `reference_relationship.portrait`）。
2. **出图组合**：WAI v170 + Iwao + akaimelon，采样参数照 `config/style.json`，分辨率按景别取 style.json 的映射。
3. **拼装**：按 v1 现行的方式拼出身份块、服装块、场景块和镜头块。直接导入 `src/storycg` 里现有的函数，用一个合成的分镜条目（框架、视角、动作、表情、场景、服装等字段）驱动，不要复制或改写拼装逻辑。镜头块里的动作和表情 tag 必须是 Danbooru 上实际存在的 tag，用 v1 现有的 tag 解析和存在性检查，并记录查证来源。
4. **场景**：每项选一个最普通、和动作相符的日常场景（例如坐椅子就放在室内房间，趴桌睡就放在办公桌前），两个角色用同一个场景。第 18 项是雨天室外，第 19 项是夜晚室内窗边。不要为了让模型好画去特意简化背景。
5. **服装**：默认用身份状态里的默认服装；第 20 项换成非原设服装（例如睡衣），两个角色用同一类。
6. **seed**：每个角色每项 2 个，由固定规则生成，并写进文件。
7. **产出**：
   - `probe/prompts.json`：每一项每个角色一条记录，含正向、负向、四个块各自的 tag、seed 列表、宽高、LoRA、IPAdapter 参考、tag 查证记录，以及可以直接提交 ComfyUI 的完整工作流。`filename_prefix` 以 `probe/` 开头。
   - `probe/prompts.md`：给监工审阅的表格，每项一行，两个角色并列，写出场景块和镜头块的 tag（身份块和服装块可以只在文件开头写一次），并标出哪些 tag 是解析器替换或丢弃过的。

## 不许做

出图；调用视觉模型；修改 `src/`、`skill/`、`config/` 等已有代码和配置；git commit。

## 预算

出图 0；时长 40 分钟。

## 必须产出

`probe/prompts.json`、`probe/prompts.md`、`reports/CAP-01.md`，报告里写验证方法，以及不确定的地方，例如哪些动作没有精确对应的 tag。
