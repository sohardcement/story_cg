# P9-06 续做（监工）

你按规则停下是对的。监工随后从本机直接访问同一地址得到 HTTP 200（`https://danbooru.donmai.us/wiki_pages/tag_group:posture.json`），你遇到的 403 是 Cloudflare 的临时挑战页。

1. **姿态词表来源**：先自己再访问官方地址（可以间隔重试几次）；若仍是挑战页，使用监工代为抓取的缓存 `verify/danbooru/tag_group_posture.json`（只读；来源 URL、抓取时间、sha256 见同目录 `tag_group_posture.provenance.json`）。无论用哪份，都在你的缓存里记录实际来源与 sha256。不要手写词表。
2. **D18 的实现位置**：`src/storycg/tiers.py` 在 v2 冻结清单里，不要改它。把"unverified 项不得否决层级"的政策放在冻结文件之外的层级门禁/编排代码里（对冻结函数的原始逐项结果做后处理），原审核记录保持不变。
3. 其余按原工单继续：重复主角修复（让 `protagonist_count` 诊断只能选到已有的去重指令编辑；在 schema 或映射层约束，不改冻结循环文件的话同样放在冻结文件外）、跨块姿态、possession_swap，然后三组回归。若某项必须改冻结文件才能实现，停下说明，不要改。

在 reports/P9-06.md 追加「续做」。预算与原工单相同（出图 ≤ 90，工单时长 200 分钟，从现在起算）。
