"""Write this order's report from actual persisted evidence, including gaps."""
import json
from pathlib import Path
from datetime import datetime
root=Path('reports/assets/P6-02');ledger=json.loads((root/'live-usage.json').read_text())
candidates=json.loads((root/'usable-candidates.json').read_text())
latest={r['tag']:r for r in ledger['results']};collages=json.loads((root/'collages.json').read_text())
l3=[r for r in latest.values() if r['tier']=='L3'];generated=[]
for p in Path('runs/p6_02').rglob('state.json'):
 for a in json.loads(p.read_text())['attempts']:
  if a['status']=='completed':generated.append((p,a))
rows=[]
for c in candidates:
 r=latest.get(c['tag'],{});job=Path(r.get('job',c['job']));ident=json.loads((job/'identity.json').read_text())
 refs=ident.get('tier_evidence',{}).get('references',[])
 rows.append('| '+ ' | '.join([c['tag'],c['created_at'],str(c['g_solo_count']),str(c['references']),r.get('tier','未运行'),', '.join(str(x['post_id']) for x in refs),str(job)])+' |')
pytest=(root/'pytest-final.txt').read_text().strip().splitlines()[-1]
started=datetime.fromtimestamp((root/'release.json').stat().st_mtime).astimezone();now=datetime.now().astimezone()
text=f'''# P6-02：L3 参考注入与 L4 诚实说明

**{'L3 已有真实产物，仍需监工看图验收。' if l3 else '部分完成。L3 代码和离线分支覆盖已实现，但本轮尚未找到被当前 L2 审核判为失败的合格角色；不能声称真实 L3 成功。'}** L4 两个真实资料用例都返回 needs_user，未生成任何人物图。没有修改审核阈值、可靠性配置或任何原始视觉结论。

## 实现

- `src/storycg/fallback.py`：L3 面部定位/裁切、IPAdapter plus-face SDXL 注入、逐来源 tag 比对、最多初次加两次诊断重试、通过后的 portrait.json 与 D5 面部参考交接，以及 L4 needs_user。参考按 P6-01b 返工后的排名选第一张用于注入，所有下载参考参与比对；post id/URL/hash 保存。可靠外貌必须全部 pass；默认服装主体适用时，可靠服装项也必须 pass。未可靠项目保留原始结果与限制，不当身份认证证据。
- 参考只用于生成和比对；外貌与默认服装字段继续来自原 wiki/共现资料，未从图像补写原设。注入前核验实时节点/模型与参考 hash，不安装节点、不下载模型。
- `config/identity_rules.json`：L3 最多 3 次、外貌失败时面部权重 0.55→0.65→0.75、服装失败时来源服装提示权重 1.0→1.2→1.4、视觉预算 18、900 秒。诊断理由是 L2 的可靠外貌失败后增强面部条件；同一 seed、同一来源身份块和服装、同一画风，仅依诊断改变条件。可靠服装失败但外貌通过时，保留面部权重，只加强失败的来源服装 tag；不自由追加服装元素。权重高于故事镜头的 0.4 是定妆恢复所需的待验证假设，不声称它已取得真实改善。实时机制检查见 ipadapter-live.json，节点与所需模型可用；这不代替出图效果验证。
- 每组参考 hash 与锁定身份/服装对应独立 L3 session，更新参考不覆盖旧尝试/图片；参考 crop、上传、workflow、逐次比较与诊断均持久化。输出使用任务名和 session hash 的唯一前缀。通过的定妆记录先落盘、再更新 identity，离线模拟这两步间中断后不重复提交。
- `src/storycg/tiers.py`：现有 L2 失败连接 L3；缺少足够参考先返回 L4；L3 可以使用单张“用户提供”参考，记录原路径/hash。传输、schema、hash 等错误不当作底模不认识的证据，不借此加大权重重复出图。缓存 L2 失败可续走 L3；L1/L2 原始比较规则、来源输入、版本及可靠性没有变。
- `src/storycg/pipeline.py`、`__main__.py`：L3 可进入正式 make/run，直接复用已审核 L3 定妆；后续镜头仍 IPAdapter 0.4。L4 返回 needs_user、原因与用户可提供的参考图路径／角色 tag／作品名，没有剧情图尝试。
- `src/storycg/identity.py`：没有角色候选返回 L4/needs_user；多个候选及作品过滤不匹配仍保留原消歧行为。
- `src/storycg/agent.py`：正式 make/resume 接受 `--reference-path`、`--tag`、`--work`；L4 说明传至 Hermes facade。用户补充信息仅在无故事提交且 worker 未活动时写入；纠正 tag 后旧分镜和调用日志保存在 edits 下，重新规划而不沿用旧服装，历史调用仍计预算。没有安装或修改已安装的 Hermes skill。
- `tests/test_p6_02.py`：离线服务边界夹具覆盖成功/失败/缺机制/缺可靠资料/参考变更/传输错误/中断恢复/L4 门禁，以及 L3 × none/qwen 的四镜 D5 连接。夹具图片不是实测证据。
- 新工具 `tools/p6_02_*.py`：按数据查候选、资料解析与严格参考筛选、串行预算实测、拼图、只读核对、L4 无提交回放与报告。

## 候选选择与实际层级

WAI v17 实时 API `https://civitai.com/api/v1/model-versions/2883731` 返回 name=v17.0，publishedAt=**2026-04-23T13:02:02.382Z**，checkpoint 文件名与项目一致。原响应在 `reports/assets/P6-02/release.json`。

按 Danbooru character 类别、创建时间降序、post_count>=30 查询，再逐个请求 `counts/posts.json?tags=<tag> rating:g solo`，只接受创建晚于发布时间且 g/solo>=30 的 tag；随后用现有 resolver 取来源资料、现有严格参考筛选器取参考。没有靠印象选角色，也没有按生成效果手挑图。查询 URL、原始 tags/counts、候选筛选结果在 `candidates.json`、`candidates-expanded.json`、`tags-page-*.json`、`expand*.log`。本项目旧 identity 的已有角色仅菲伦、芙莉莲、月城柳、约尔，本轮候选均未使用过。

首批 8 个合格计数候选中，多数严格参考不足；achichi_mela、suzuna_tsuzuri 虽参考足够，却没有锁定且可靠的发色字段，不能验证身份。继续按同一日期顺序扫描，取前 7 个同时具备来源发色与至少两个严格参考的候选，保留 3 张预算给可能的 L3：

| tag | Danbooru 创建时间 | g solo 数 | 严格参考数 | 真实结果 | 比对参考 post id | 任务目录 |
|---|---|---:|---:|---|---|---|
{chr(10).join(rows)}

每份实际 identity.json 的 tier_evidence 包含 L2 测试 path/hash/prompt_id、无参考 workflow、逐 tag 原始两图观察、可靠/不可靠项、参考 URL/hash、LoRA 匹配与参数。正式 L2 测试仍是来源身份块+默认服装+中性 tags，无图片输入。这测试的是完整来源提示下的复原，不是纯角色 tag 的知识探针。

**关键限制：当前外貌 reliable 项只有发色。Phoenix Reffaella 的来源提示已经明确给出 blonde_hair，脚本因此把发色一致判为 L2；本地查看对照时，发饰、头发轮廓、服装与参考却有明显区别。因此本轮 L2 通过不能证明底模真正认识完整角色，也不能据肉眼疑点把原始 pass 改成 fail 或强行走 L3。** 若最终候选均通过，缺少的正是工单要求的真实 L2 失败证据。部分候选是新建的服装/形态 tag，tag 创建晚也不等于底模从未见过其基础角色；没有把创建日期代替测试证明。已有角色排除核对见 unused-character-check.json。

后续阻塞应由监工明确：L2 是否需要另加不带来源外貌提示的纯角色 tag 知识探针，或先校准整体身份/标志特征的审核。本单没有自行更换 P6-01b 的输入协议或可靠项，更没有用新的解释改写已有 pass。

## L4 真实用例

1. 随机组合中文名 **岚砚柚澄霁珞**：Danbooru autocomplete 无候选；中文维基精确搜索 `srsearch="岚砚柚澄霁珞"` 返回 totalhits=0、search=[]，精确标题查询也返回 missing。首次不加引号的 API 搜索曾 403，保留原失败；后来请求成功，不把 403 当查不到。证据：`runs/p6_02/l4/unknown/cache/`、`chinese-wikipedia-exact-search.json`、`chinese-wikipedia-retries.json`。输出：`needs-user.json`，原因“找不到该角色的资料”。
2. **frederica_irving_(sekaiju)**：真实存在 tag，g solo=38；按既定 min_score=20/排除变体/solo/外观排序规则得到 0 张可用参考。筛选原响应和淘汰原因在其候选目录 `tier/probe_*/references_v2/`，正式输出 `runs/p6_02/l4/insufficient/needs-user.json`，原因“底模身份未验证且没有足够的可靠参考图”。没有先生成一张无法比较的角色图。

两任务没有 state.json，没有生成 PNG，也没有 ComfyUI prompt_id。`tools/p6_02_l4_check.py` 用真实已解析输入重走正式 pipeline，并在 submit_prompt 边界设禁止提交记录，结果两者 submission_attempts=0。此回放验证门禁，不作为新出图或视觉效果证据；记录在 `reports/assets/P6-02/l4-no-submissions.json`。

## 产物与可复查命令

- 真实测试与参考：`runs/p6_02/live/` 和 `runs/p6_02/candidates/phoenix_reffaella/`；下载清单均含 post/查询/file URL/hash，完整原设证据仍在各 identity.json 的来源字段。
- 实际提交/视觉用量总账：`reports/assets/P6-02/live-usage.json`；只按其中自己的 prompt_id 查 history，不清队列、不 interrupt。
- 拼图索引：`reports/assets/P6-02/collages.json`。{'存在 L3 拼图，见索引。' if l3 else '当前仅有 review/P6-02-L2-<tag>.jpg；没有伪造 review/P6-02-L3-<角色>.jpg。'}
- 实际逐项审核：各运行目录 review_log.jsonl 与 tier/probe_*/l2/comparison-v2.json；所有原始结论保留。
- 冻结文件核对：`reports/assets/P6-02/frozen-hashes.json`；review/reference/calibration 与 reliability 对 HEAD 的 hash 全部一致。

以下命令不提交新生成或新视觉审核，可直接复制：

```bash
uv run pytest -q
uv run python tools/p6_02_l4_check.py
uv run python tools/p6_02_collage.py
uv run python tools/p6_02_check.py
git diff --check
```

全量结果：`{pytest}`，日志 `reports/assets/P6-02/pytest-final.txt`。红绿日志均在同一 assets 目录。产物核对结果在 artifact-verification.json；若其中 pending 非空，代表执行时还有自己的任务在进行，不能声称已全部核对。

本地参考调用：

```bash
uv run python -m storycg make jobs/NEW_JOB_ID --character '角色名' --tag '标准角色tag' --reference-path '/本地参考图路径' --story '剧情' --foreground
uv run python -m storycg resume jobs/EXISTING_L4_JOB --reference-path '/本地参考图路径' --foreground
```

这些是生成命令，会消耗用户的新任务预算；本单没有把它们作为免预算检查命令执行。仅给一张图、没有可追溯的角色/tag 外貌资料时仍保持 needs_user，不能从图像反推原设。

## 用量、未完成与合规

- 实际生成 **{len(generated)} 张**，提交 **{len(ledger['submissions'])} 次**，实际视觉调用累计 **{ledger['vision_requests']} 次**（request-count-check.json 已与全部真实 HTTP request 事件核对）。本地查看拼图目前 1 次；下载参考不是出图。没有剧情图。
- 记录窗口从首个外部 API 产物 {started.isoformat()} 到报告 {now.isoformat()}，约 {(now-started).total_seconds()/60:.1f} 分钟；开工阅读在这之前，额外约 3 分钟，总耗时为估计。ComfyUI/视觉服务与监工 Hermes 共用，真实耗时可能受并发影响。单个候选用时已记录，不能把等待解释成自身模型性能。
- 工具初次用了含括号的 tag 作 job ID，前四个探针在提交前因 ID 规则停止。错误 identity 与日志保留，随后复制同一来源资料和已筛选参考到安全的 p6_02_01..04 目录，没有为此生成重复图、换 seed 或改变参考筛选。Phoenix 的合法目录成功运行，后续复用缓存。
- {'真实 L3 完整证据仍需监工看图判定，不把可靠发色通过视为整体身份认证。' if l3 else '未完成：真实 L3 成功样本、L2 失败→L3 定妆的完整证据链、指定三列 L3 拼图。当前不能满足工单的 L3 看图验收。代码已实现不等于参考注入效果已验证；不降低阈值、不改判、不把旧图或离线夹具充当此证据。'}
- 尚未在真实 make 中生成一组 L3 剧情，用户本地参考路线仅有离线分支验证；没有声称其实际画面效果达标。参考筛选依赖 Danbooru 标签，缺失/误标和来源共现的默认服装抽取误差仍可能存在。
- 未 git commit，未写 verify/，未修改旧 skills/旧项目代码/已有作品、ComfyUI 配置/节点/模型、TASK/SUPERVISOR/PROGRESS/orders/AGENTS。开工 orders/P6-02 修改及 jobs/logs/verify 的并发变化不是本单写入。监工期间另改了 agent.check_resources 的 timeout；该行保留，不归作本单改动。
'''
Path('reports/P6-02.md').write_text(text)
print('wrote reports/P6-02.md')
