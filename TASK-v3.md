# TASK v3（草稿，2026-10-19，监工起草，待用户定稿）

v2 于 2026-10-18 验收冻结（`TASK-v2.md` 顶部）。本稿的起因：2026-10-19 用户让 Claude 以普通用户身份、只按 SKILL.md 跑一遍 /story-cg（任务 `jobs/cg-ed4224ca2074`，御坂美琴自动贩卖机 4 镜，出图 23 张；走了 redo-portrait、`--more`、出图中 cancel 再 resume、reshoot、deliver、完成后换镜），记下约 20 处会让照文档操作的人或 agent 走偏的问题。v3 的目标就是把这些关掉；画质、模型、范围都不动。

定稿前以 TASK-v2 为准；定稿后本稿和 TASK-v2 冲突时以本稿为准。用户自己真正想画的两三条剧情（v2 顶部"下一个该量的"）仍然是下一个要量的东西，不在本稿范围；建议在本稿第 ① 批做完后再跑，少踩文档和命令的坑。

## 0. 目标与范围

目标：写作说明、SKILL.md、命令行为、交付说明四者一致；每条错误路径都有调用方能照着走的出口；不改出图结果。

v3 做：
- 命令行为的缺陷（§3）、文档的不实和自相矛盾（§4）。出图 0 张，回归测试加离线重放。
- 三件要用户定的事（§2）。其中改提示词的，按通用规则②走 D114 基线重跑。
- v2 遗留②③并入第 ③ 批的基线重跑（§8）。

v3 不做：画质改进、LoRA、换底模、双人同框、道具（v2 §0 冻结照旧）、Hermes 批量。不加新的出图功能。

优先级：不改出图结果的修复 > 文档 > 要用户定的 > 出图验证。

执行：在 `v3` 分支（工作树）上改，主线 `master` 保持 v2 冻结状态，已安装的 skill 继续指向主线，直到第 ③ 批验收通过（沿用 v2 §8 的做法）。执行者守则 AGENTS.md 第 9–11 条照旧；工单编号 `V3-nn`。

## 1. 证据

- 任务 `jobs/cg-ed4224ca2074`：`beats.json`、`storyboard.json`、`candidates.json`、`manifest.json`、`result.md`、`worker.log`、`delivery/`（拼图和候选总图）。这是主要证据，不动。那次用的分镜也在 `drafts/cg-ed4224ca2074/beats.json`。
- 两个探测任务没出图、留作回归用例、不删：`jobs/cg-e4eb06f59cee`（清单外角色，停在 needs_user）、`jobs/cg-0a0964bf29cf`（歧义名，resolve 后停在 identity_confirmed）。
- 本次会话的 59 个 `compile/cmp-*`（2026-10-19 15:03–15:27，`find -newermt` 数的）里有每次探测编译出的 `storyboard.json` 和 `plan_log.json`，可作旁证；prune 之前别删。
- 命令序列、各步 stdout/stderr、探测用的 beats 文件当时放在 `/tmp/storycg-probe/`，**起草本稿时该目录已不在**（/tmp 被清），没有进仓库。能恢复的：写作说明第 1 节示例原样就是 `example.json`；其余探测 beats 按 §3、§4 各条写的字段和 tag 重写，每条都够重写。监工已于当晚重建到 `verify/v3/dogfood-2026-10-19/beats/`（28 份，README.md 列了每份探的是什么和改前行为），改前输出在同目录 `before/`（`snapshot.py` 跑的；改后对 `.v3` 再跑一遍作对照）。
- 发现清单按影响排序在 2026-10-19 的会话回复里；本稿按"决定 / 缺陷 / 文档"重排，编号给工单引用。

## 2. 要用户定的三件事（定了之后从 D118 编起）

### 2.1 表外机位：改写还是照画

现状有三条规则：§9 第 2 条（2026-10-09）说 pose 在词汇表里有稳定组合时，改用那个组合的景别和角度；D96 第 6 条说表外组合标"未测"、不占名额；D104 ①说"写了 `framing: wide` 照画，不改写"。代码实现的是 §9 第 2 条（`beats.py` `rewrite_board` 规则 1；D104 ③只把 wide 排到候选最后）。写作说明第 5 节印的是 D96 的话："表里没有的组合可以用，记为未测，不占名额。"

实测（compile，御坂美琴）：`wide/side/sitting`、`closeup/front/standing`、`full/dutch/standing`、`medium/side/standing` 全部被改成 `medium/front`；只有 pose 本身表外的 `leaning_forward` 保留了 `wide/side`。整组四镜变成同一机位，改写只出现在 rewrites 文本里。交付后 `reshoot --framing wide --angle side`（D102）不走这条改写，第 4 镜因此拿到了真正的远景侧面（第 2 轮 3 张都是）。也就是现在想要机位变化，只能先出完一轮再 reshoot。

选项：
- **A（推荐）**：§9 第 2 条收窄成"补全"：作者省略 framing 或 camera_angle 时才按稳定组合补；作者写了的照画，分档记"未测"，不占名额，rewrites 记一条"表外组合，未测"。和 D96 第 6 条、D104 ①一致，写作说明第 5 节的话就成真的；reshoot 已经证明表外机位画得出。
- **B**：保持改写，把写作说明第 5 节改成"表内 pose 的表外机位会被改成稳定组合，想换机位交付后用 reshoot"。不动代码。
- **C**：A 的行为加一个开关（配置 `compose.rewrite_off_table`），默认关，diagnostic profile 打开。

影响：A 和 C 改编译结果，属于"改默认"，按通用规则②跑 D114 基线。已核对 `verify/v2/d114-fidelity/beats/` 六份固定分镜：**s01 第 2 镜（sitting，medium/side）和 s08 第 2 镜（standing，closeup/front）是表内 pose 配表外机位**，按规则 1 会被改成 medium/front；选 A 或 C 这两镜的提示词会变，基线要重跑，和 v2 遗留②③合成一次。其余四条里的表外镜（s05 jumping、kneeling；s06 leaning_to_the_side、jumping、landing；s04 holding_cup 是"有时"档没有稳定候选）都不受这条影响。

### 2.2 男角色

现状：`identity.py` 的 identity_block 写死 `1girl`，negative 固定 `multiple girls, 2girls`。上条当麻编译出来是 `1girl, kamijou touma, …`。SKILL.md 和 v2 §0 只说"一个具名主角"，没说限女性；WAI 清单里有男角色。

选项：
- **A（推荐）**：身份解析阶段判性别：用已有的 `related_tag` 查询结果里 `1girl` 和 `1boy` 的共现帖数，多的一方进 identity_block；两边接近（差不到 3 倍）或都没有时 needs_user 问一次，`resolve --gender girl|boy` 回答。negative 跟着换（`multiple boys, 2boys`）。写作说明和 result.md 不用改。
- **B**：不支持男角色：清单闸门之后加一道，`1boy` 帖数明显多的角色返回 needs_user，reason `male_character_not_supported`；SKILL.md 写明"只支持女性主角"。
- **C**：调用方传 `--gender`，默认 girl。调用方多半不会传，等于现状。

影响：A 只改男角色的提示词，D114 的六条都是女角色，不用重跑基线；用上条当麻跑一组 4 镜看图判（约 15 张）。B 出图 0 张。


## 3. 直接修的缺陷（出图 0 张；回归测试 + 离线重放）

每条写"现状 → 改成"。

### 3.1 命令行为

1. **cancel 成功却按失败报**：取消生效后 exit 1，`error` 写"worker 收到信号 SIGTERM 后退出；可以运行 resume 接着跑"，接下来的 resume 输出还带着这条。SKILL.md 要"有 error 时原样说明"，照做就把一次正常取消讲成故障。→ cancel 成功 exit 0、`error` 为空、`phase` 为 cancelled，退出原因放新字段 `stopped_by`（cancel / signal / error）；resume 启动时清掉上一次的 `error`。`stopped` 只留给非 cancel 的信号退出。
2. **cancel 吃掉 `--more`**：出图中 cancel 再 resume，`more` 被直接记成 `more_done`，候选仍是 3 张，没有提示（`candidates.json` 第 2 镜 `more: 1, more_done: 1`，第二次 `--more` 才补出来）。→ 打断时 `more > more_done` 的请求保留，resume 接着出；打断时留下的孤儿图照现在的做法接着用。
3. **compile 对歧义名和清单外角色没有调用方能走的出口**：歧义名返回 `identity_not_locked` 加候选列表，没有顶层 `needs_user`、`question`、`options`、`job_dir`，不接受 `--tag`；清单外角色只有一个 `status: needs_user` 字符串，没有 `needs_user` 布尔。SKILL.md 说"只在 needs_user 为 true 时提问"，字面执行就卡住。→ compile 的 needs_user 输出和 make、status 同形（顶层 `needs_user: true`、`reason`、`question`、`options`）；compile 接受 `--tag`、`--work`（同 make、resume）。
4. **stdout 不总是 JSON**：beats 文件不存在、`--shots 3`、profile 未知时 stdout 为空，只有 stderr 一行。→ 一律输出 `{"status": "error", "kind": …, "message": …}`（`agent.py` 788 行已有这个形状），exit 1。
5. **status 的错误是 Python 异常原文**：工作区外路径直接吐 `ValueError: agent job paths must be inside the project workspace`；对 compile 留下的目录返回 `phase: unknown`，compile 和 make 的输出都叫 `job_dir`，很容易拿错。→ 两种都返回 JSON error，文案说人话（"不是任务目录"、"这是 compile 草稿目录，不是任务"），exit 1。
6. **还没选就标"已选"**：每镜停下时 `candidates.json` 预写 `selected: 1` 和一条"按 candidate number 分出胜负"的理由（`candidates.py:338`），拼图标 SELECTED，`progress.picked` 为 true（`agent.py:609` 只看 selected 非空）；reshoot 后新旧两种机位的 6 张混在一张拼图里，默认仍指向旧机位的候选 1。→ pending 阶段 `selected` 为空、拼图标 `default`、`progress.picked` 看 `confirmed`；reshoot 后 `default_candidate` 指向新一轮第 1 张，拼图按轮次分行。
7. **换回默认候选后理由被抹掉**：completed 后把第 1 镜换成候选 2 再换回候选 1，result.md 的理由退化成自动理由，看图挑选的事实消失；换镜文案写"审核器原选候选 1"，这个模式里没有审核器。→ 只要 `picked_by` 存在就写"由调用方/用户看图换回候选 1"；"审核器"改成"程序默认"。
8. **reshoot 不记操作者**：story_note 写"第 4 镜由用户改为 wide/side 重出"，实际是 agent 调的。→ reshoot 加 `--by {user,agent}`（默认 user，同 pick），说明按它写。
9. **镜数边界不一致**：beats-guide 接受 `--shots 2` 和 `7`，compile 和 make 拒绝 4–6 之外。→ beats-guide 同样拒绝，用同一条文案。
10. **prune 不管 drafts，`--days 0` 被拒**：每次 beats-guide 留一个空的 `drafts/cg-*`（本次会话 6 → 16，12 个空），每次 compile 留一个约 220K 的 `compile/cmp-*`（805 → 849）。→ prune 增加"drafts 下的空目录"一类（不看天数）；`--days 0` 允许；照旧先列后删。

### 3.2 编译与提示词

11. **普通 compile 放行的三类**：同一场景 `indoors` 加 `outdoors`；action_tag 用表外词（`throwing`、`holding_phone` 直接进提示词，D113-b 把 action_tag 做成封闭枚举，但只有 `--strict` 查 schema）；多余的 `spatial_relation`。Hermes 跑的是不带 `--strict` 的 compile。→ 按 D96 第 1 条"能改写的改写"：表外 action_tag 按 `fit: approx` 处理（丢弃，意图留 action_note，记 rewrite）；`spatial_relation` 丢弃并记 rewrite；室内外互斥报错（改不了，fix 里给两种写法）。`--strict` 的行为不变。
12. **错误分两批报**：引用错误（scene 未定义、镜数）先报，schema 错误后报，要改两轮。→ 一次收集两类再输出。
13. **reshoot 的 negative 没按新景别重算**：第 4 镜第 2 轮 positive 有 `wide shot`，negative 也有 `wide shot`，并且 `multiple girls, 2girls, multiple views` 重复两遍。→ 改机位后 negative 按新景别重算（compile 预览里 wide 镜的 negative 本来不含 wide shot）；negative 去重。不动 medium 镜的提示词，不触发基线重跑。
14. **标准服装 tag 互斥**：`summer_uniform` 和 `winter_uniform` 一起进了每镜提示词；三种袜子 tag 在中景被 `drop_leg_tags` 丢掉、远景全留，写作说明没有一处提到。→ 这条改提示词，和 §2 一起由用户定：a) 互斥组（夏冬装、袜长）按 Danbooru 帖数留一个；b) 保持现状，只在写作说明第 4 节写明。选 a 要跑 D114 基线（先核对 D114 的角色里有没有互斥 tag）。

### 3.3 交付说明 result.md

15. **引用不存在的 review.md**（"详细审核：见 review.md 和 manifest.json"）。→ 只在文件存在时写；没有视觉审核时写"没有 review.md"。
16. **"已知问题"把整段 story_note 原样塞进去**：原剧情加全部 rewrites 的内部术语（composition_reason 之类）。根源在 `beats.py:85` 把 rewrites 拼进 story_note，status 的 `confirmation.story_note` 也跟着脏。→ storyboard 存 `rewrites` 列表，`story_note` 只保留作者的话加 reshoot 备注；result.md 加一节"编译改写"逐条列（和 status 的 `compile_rewrites` 同源），"已知问题"只写作者的 story_note。

## 4. 文档（出图 0 张；加核对测试）

### 4.1 写作说明（beats-guide）

- 第 1 节示例补齐 `framing`、`camera_angle`、`fit`；加测试：示例原样过 `compile --strict`（现在报 10 条错误）。
- 第 5 节稳定表里的 `on_back`、`animal_hug` 是第 3 节的禁用 pose；"不可用 12 → 11"的替代是 `holding_cup`，第 3 节说带物件的 pose 禁用；普通 compile 放行，`--strict` 才拒绝。→ 打印词汇表时过滤禁用 pose 的条目（或标"禁用，不要用"）；替代指向禁用项的改成"无替代，按无法可靠表现处理"。`config/shot_vocab.json` 不动。
- 第 4 节写明 `drop_leg_tags`（D116）：中景、近景丢鞋袜 tag，远景、全身保留；互斥服装 tag 按 §3.2 第 14 条的决定写。
- 第 6 节同一句括号说明重复 7 次 → 合并成表头一句。"撞键说明"改白话；`fit` 重写，给一正一反两个例子。
- 第 5、6 节对所有角色完全相同 → 标题去掉角色名，说明是全局表。
- 2.1 定了 A 或 C 时，第 5 节"表里没有的组合可以用，记为未测"保留；定了 B 就改成实际行为。

### 4.2 SKILL.md

- 补：`write-beats`（或写明"不对调用方开放"）、`--strict`、`--foreground`、`--verbose`、`--seed`、`--identity-control`、`--config`、`diagnostic` profile；阶段名全集（queued、resolving_identity、checking_identity、planning、preparing_portrait、awaiting_approval、generating_and_repairing、awaiting_pick、completed、needs_user、identity_confirmed、cancelled、stopped、failed）；stdout 规则（§3.1 第 4 条做完就是"永远 JSON"）。
- 歧义名的路径写清：compile 返回 needs_user 时按 options 问用户，`compile --tag` 或 `make --tag` 继续。
- 加测试 `tests/test_skill_doc.py`：SKILL.md 里出现的每个子命令和参数在 `--help` 里存在；`--help` 里的每个参数在 SKILL.md 或 SKILL.md 的"不对调用方开放"清单里。和 `test_defaults_doc.py` 同类。

## 5. 验收

- **出图 0 张的（§3 除第 14 条、§4）**：全量 pytest 通过；离线重放（D93 第 5 条）；把 `jobs/cg-ed4224ca2074` 复制到 `.pytest-tmp/` 重建交付，result.md 的差异只在预期行（§3.3），原任务 sha256 不变（照附录记录①的做法）。
- **行为类脚本** `verify/v3/dogfood/check.py`：把 2026-10-19 的命令序列做成断言。不出图的部分直接跑：示例过 `--strict`、歧义名 compile、清单外 compile、status 错路径、`--shots` 边界、prune 列表、stdout 永远 JSON。需要出图的用一次 4 镜任务，同一条剧情同一份 beats，约 20 张：cancel 不丢 `--more`、pending 不预写 selected、reshoot 的 negative、reshoot `--by`。
- **改提示词的（§2.1 选 A 或 C、§2.2 选 A、§3.2 第 14 条选 a）**：按通用规则②，改完跑一次 D114 六条基线，和 v2 遗留②③合并，用户按 D114 口径逐格判；§2.2 另加上条当麻一组 4 镜看图判。每个实验报告的"限制"段按通用规则③写：seed 数、角色数、场景数、有没有对照臂。
- **最终**：Claude 再以用户身份、只按新 SKILL.md 跑同一条剧情（同 beats、同 seed）加一条换装剧情，对 2026-10-19 的 20 条逐条写"已修 / 未修 / 不改（原因）"；result.md 和 SKILL.md 对读没有不实。

## 6. 顺序

1. **第 ① 批（不等用户，出图 0 张）**：§3.1 第 1–10 条、§3.2 第 11–13 条、§3.3、§4。改前基线见 §1。工单拆分：V3-01 命令输出同形（第 3、4、5、9 条）；V3-02 cancel / resume / `--more`（第 1、2 条）；V3-03 选图预写与 reshoot 后的默认（第 6 条）；V3-04 换回理由、reshoot `--by`、prune、review.md（第 7、8、10、15 条）；V3-05 编译放行与错误分批（第 11、12 条）；V3-06 reshoot 的 negative（第 13 条）；V3-07 story_note 拆分与两份文档（第 16 条、§4，监工或强模型做）。机械改动派 Haiku 工单（AGENTS.md 第 10 条）；story_note 拆分（第 16 条）、SKILL.md 和写作说明的文字由监工做。每张工单 30–60 分钟，宁可拆细。
2. **第 ② 批（等用户定 §2 和第 14 条）**：定了再派。2.1 选 B、2.2 选 B 时出图 0 张。
3. **第 ③ 批（出图）**：行为类脚本一次 4 镜；§2 需要的组；D114 基线重跑并入 v2 遗留②③。
4. **收尾**：§5 最终复跑；改了默认的话更新 `config/DEFAULTS.md`；合入主线；`tools/install_hermes_skill.sh` 重新检查。

## 7. 预算

| 批 | 出图 | 说明 |
|---|---|---|
| ① | 0 | 回归测试、离线重放 |
| ③ 行为类脚本 | 约 20 | 一次 4 镜，含 `--more`、reshoot |
| ③ §2.2 男角色 | 约 15 | 一组 4 镜，只在选 A 时 |
| ③ D114 基线重跑 | 按 D114 的量，约 80 | 和 v2 遗留②③合并，只跑一次 |
| 收尾复跑 | 约 25 | 同剧情加一条换装 |

合计不超过 150 张，单独计数，登记在 PROGRESS 出图用量表。超出先报用户。

## 8. v2 遗留并入

- ①道具里的角色名：已做（e901a1d1）。
- ②closeup 模板去掉 `upper body`、③`camera_tags.front` 填 `facing viewer`：按 v2 顶部的安排，和第 ③ 批的 D114 基线重跑合在一起做，不单独开实验编号。

## 9. 不做、不确定、没测到

- 不做：见 §0。
- 不确定：2.2 选 A 时性别判定对 WAI 清单里的角色有多少判不出来（先离线抽查前 200 个 tag 的 related_tag）；§3.2 第 14 条 D114 的角色里有没有互斥服装 tag（要读身份缓存，第 ① 批顺带查）。
- 没测到：6 镜、换装剧情、`--no-confirm`、`write-beats`、`host_warning` 真触发时的表现。前两个放进 §5 的最终复跑。

---

## 附：v2 冻结后的记录（原 TASK-v3.md 的内容，按时间续写）

v2 已于 2026-10-18 验收并冻结（`TASK-v2.md` 顶部「v2 验收结论」，当前默认见 `config/DEFAULTS.md`）。v3 的目标还没有定：等用户跑完自己真正想画的两三条剧情，看离“能发出去”还差什么再定。在那之前，这里按时间记 v2 冻结之后的改动和新问题。

## 记录

- **result.md 两处不实（用户，2026-10-19：“执行修复吧”；Claude 做，出图 0 张，无审核）**。起因是 Codex 试跑（`verify/harness-codex/`，f74c22c4）。
  - ①「审核」一节：人选模式下固定写“这一组由用户逐镜看图选择候选……以你看到的为准”，四镜都是 `pick --by agent` 时也这么写。现在按每镜的 `picked_by` 写（`candidates._human_review_line`）：全是用户选的照旧；全是调用方选的写“这一组由调用方（调用本 skill 的 agent）逐镜看图选择候选，用户没有看过候选……请你看图确认”；两者都有时逐镜写明谁选的。同一类的三处小句也跟着改：选中的候选没通过筛选时“是用户指定的”“（用户指定保留）”改成按实际选图的人写；“用时……（不含等你看图和确认的时间）”改成“不含等待看图选图和确认的时间”。没有 `picked_by` 的旧任务按用户选的处理（`--by` 是 loop B 才加的）。
  - ② `--more` 之后的轮次：result.md 原来写的是 slot 的 `rounds`（只数自动轮，人选模式下一直是 1），现在写选中候选自己的轮次；加出的一批原来编号 `2 + more_done`，没有自动第二轮时跳过 2，现在是这一镜已有的最大轮次加 1。旧任务里已经记成第 3 轮的候选不改，result.md 照记录写第 3 轮。轮次不进种子（`candidate_seed(base, shot, candidate)`），所以出图不受影响。
  - ③ 检查脚本 `verify/loop-b/check_job.py` 的 `result_honest`：原来只查“由用户选择”，现在也查“由用户逐镜看图”“由用户换”。**用修正后的检查回放**：loop B 交付了的 8 次（第 1–3、5–9 次）和 Codex 第 1 次全部不过这一项，句子都是模板写的，不是调用方写的；第 4 次没有交付，没有 result.md。所以 v2 结论里“loop B 连续 5 条通过”在这一项上不成立，已在 TASK-v2 顶部加了更正。
  - ④ 我在 28ba1efc 留下的测试泄漏：`test_v2_38`（进程内）和 `test_v2_34`（子进程）跑 `beats-guide` 时在项目的 `drafts/` 下各留一个空目录。conftest 加了自动夹具，进程内的测试都把 `DRAFTS` 指到 `tmp_path`；`test_v2_34` 跑完删掉它建的那个目录。跑完相关测试，`drafts/` 里没有多出目录。
  - **实测**：把 Codex 那次的任务 `jobs/cg-8105eee4e018` 复制到 `.pytest-tmp/` 里重建交付，result.md 只变了三行（用时说明、审核一节、第 4 镜“第 1 轮” → “第 3 轮”），原任务的 result.md 没动（sha256 核对），副本已删。
  - **测试**：`test_v2_70` 新增 2 个（全部 `--by agent` 加一次 `--more`：审核一节写调用方、没有“由用户”、选中的候选写第 2 轮、`candidates.json` 里 4–6 号是第 2 轮；用户和调用方各选一镜：逐镜写明）。全量 954 passed（原文写的是占位符 TESTCOUNT；监工 2026-10-19 晚在主线复跑得 954）。
  - **通用规则②不触发**：只改 result.md 的文字和轮次编号，不改提示词、工作流、种子。
  - **看到、没改**：今天 11:26–11:29 另有调用方用了 story-cg（`drafts/` 里多了 3 个目录，其中 1 个有分镜、对应任务 `jobs/cg-a3f45279cf29`），另 2 个是空的：调用方每重跑一次 `beats-guide` 就留一个空目录。28ba1efc 的设计带来的，无害，没有清理机制（`prune` 不管 `drafts/`）。
