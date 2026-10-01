"""Append the completed rework record; refuses partial/duplicate publication."""
import json,re,sys
from pathlib import Path
from datetime import datetime,timezone
from storycg.state import save_json_atomic
assets=Path('reports/assets/P6-02-rework1');u=json.loads((assets/'usage-final.json').read_text())
assert len(u['results'])==11 and (assets/'artifact-verification.json').exists()
views=json.loads((assets/'local-image-views.json').read_text())
count=len(u['submissions']);visual=u['vision_requests']+sum(r['count'] for r in views);assert count<=18 and visual<=60
started=datetime.fromisoformat(u['started_at']);ended=datetime.fromisoformat(u['ended_at']);now=datetime.now(timezone.utc)
label={'tier_pending_L3_L4':'待 L3（未定妆验证）','tier_pending_resources':'资源待恢复'}
lines=['\n## 返工 1\n',
'本节为最新结果，前文保留原轮历史。本轮按监工 D8 重测 11 个角色；最先被否决的两个候选均完成 L3 定妆与证据链。其余角色只记录知识探针层级，不把“可走 L3”写成“已验证 L3”。\n',
'### 代码、规则与离线验证\n',
'- `src/storycg/tiers.py`：统一知识探针只用角色 tag、作品 tag 与 solo/站姿/简单背景；L1 仅另加匹配的角色 LoRA 及其训练触发词。移除来源外貌/默认服装、图片注入；固定画风保留。协议升为 v3，使用独立 probe_v3 目录，旧 v2 探针不复用。\n',
'- D8 判定：至少有一个可靠外貌项，全部可靠外貌及适用的可靠服装项须 pass；任何适用项 fail 均否决，包括不可靠/未校准项。not_visible 原样记录，不是 fail，也不能确认可靠项；不适用服装记录 not_applicable。未经可靠审核项的 pass 不作确认依据。`config/review_reliability.json`、冻结审核器和参考筛选阈值均未改。\n',
'- `src/storycg/fallback.py`：诊断重试考虑所有外貌 veto 和来源服装问题。旧 L3 缓存必须满足现版本及当前原始逐项判定，失效记录保留 history；新会话按比较版本隔离。比对通过但面部定位暂时失败时保存 pending、复用原图恢复定位，不重复生成相同图。耗尽尝试后的说明只列最后一次未通过项。\n',
'- `src/storycg/agent.py`：正式资源检查使用 `/system_stats` 与必要的 `/object_info/<NodeClass>`，同类只查一次，超时读取配置；检查匹配 LoRA、IPAdapter/CLIP Vision，及配置需要的 Qwen/修复节点与模型。L3 的机制检查也改用四个节点单项接口；LoRA inventory 只查询 LoraLoader，避免随后再次拉全表。\n',
'- `skill/story-cg-v1/SKILL.md`：新增运行时禁改代码、配置、任务状态文件；只通过 CLI 操作，原样转告 status 的 error/repair_blocked/question，可 resume，不自行修代码。没有安装 skill。\n',
'- 新增 `tests/test_p6_02_rework1.py`；更新 P6 旧测试的 canonical 输入、版本和 D8 期望，以及 `tests/test_reference.py` 的当前版本夹具。覆盖不可靠否决、not_visible、可靠服装 minor、轻量接口/超时/缺模型/Qwen 修复路径、过时或含 fail 的 L3 缓存、面部定位恢复不重复生成。离线夹具不作为实测证据。\n',
'- 工具为 `tools/p6_02_rework1{,_l4,_check,_collage,_report}.py`。按候选既定顺序串行执行、共享全局预算；只追踪自身 prompt_id，没有清队列或 interrupt。只读代码审查发现并复核关闭了上述缓存/面部定位两项问题。\n',
'验证命令（完整日志 `reports/assets/P6-02-rework1/pytest-final.txt`）：\n',
'```bash\nuv run pytest -q\nuv run python tools/p6_02_rework1_check.py\nuv run python tools/p6_02_rework1_collage.py\n```\n',
(assets/'pytest-final.txt').read_text().strip().splitlines()[-1]+'。`git diff --check` 通过。`artifact-verification.json` 核对真实提交 history、来源字段不变、workflow、图片/参考 hash、L4 零状态、冻结文件。\n',
'### 11 个知识探针的结果\n',
'新候选仍按原轮创建日期/评分筛选顺序，选择数据和 post 原响应保留在前文路径；不凭印象或出图效果另选角色。WAI 发布日期、各 tag 创建日期、g solo 计数均保留原证据。已用过的四个角色仅是监工指定的回归测试，不冒充新候选。LoRA 模型列表及 87 个大小/mtime 指纹实测未变，复用原元数据与 Danbooru 类别缓存，见 `inventory.json`、`lora-node-live.json`、`metadata-fingerprints-live.json`。\n',
'| 顺序 / tag | 原结论 | 新知识探针 | 本轮最终状态 | fail veto / 可靠未确认 | 证据目录 |\n|---|---|---|---|---|---|\n']
for r in sorted(u['results'],key=lambda x:x['index']):
 notes=[]
 if r.get('vetoes'):notes.append('fail: '+', '.join(r['vetoes']))
 if r.get('reliable_unconfirmed'):notes.append('可靠未确认: '+', '.join(r['reliable_unconfirmed']))
 if not notes:notes=['无 fail；可靠适用项通过']
 lines.append(f"| {r['index']}. `{r['tag']}` | {r['old_tier']} | {label.get(r['probe_tier'],r['probe_tier'])} | {label.get(r['tier'],r['tier'])} | {'；'.join(notes)} | `{r['job']}` |\n")
lines+=['\n每个目录的 `original-identity.json` 是原来源快照，`knowledge-result.json` 和 probe 下 comparison-v3 是实测采集记录，`knowledge-final.json` 是最终 D8 汇总；原始逐项 status/两图观察没有改写。实测进程在补齐“所有可靠适用服装亦须 pass”的确认门禁前已载入代码，因此验证工具从相同原始观察按最终规则重新汇总，并独立保存最终结果；不重新出图、不作新视觉调用。两条 L3 也经过最终规则复核，均通过。\n',
'### 最先两个 L3 的完整链\n']
for job_name in u['selected_l3']:
 job=Path(job_name);d=json.loads((job/'identity.json').read_text());k=json.loads((job/'knowledge-final.json').read_text());e=k['tier_evidence'];probe=job/e['artifact_root']
 portrait=json.loads((job/'portrait.json').read_text());ap=job/d['tier_evidence']['l3']['attempts_file'];attempts=json.loads(ap.read_text());ref=portrait['approved_identity']['tier_evidence']['l3']['reference']
 lines.append(f"\n**`{d['character_tag']}`：L3，通过第 {portrait['approved_attempt']} 次定妆。**\n")
 lines.append(f"- 纯知识探针：`{probe/e['test_images'][0]['path']}`，prompt_id `{e['test_images'][0]['prompt_id']}`；workflow `{probe/e['test_images'][0]['workflow_path']}`。原始 fail：{', '.join(sorted({x['tag'] for x in k['vetoes']}))}。\n")
 lines.append(f"- 注入参考：Danbooru post {ref['post_id']}（https://danbooru.donmai.us/posts/{ref['post_id']}），SHA256 `{ref['sha256']}`。全部参考 post/URL/hash 在 `{ap.parent/'references/manifest.json'}`；排名第一张用于面部注入，全部参考参与逐项比对。参考不作为原设，外貌/服装字段与原 wiki/共现快照逐字段一致。\n")
 for a in attempts:
  ch=a['diagnostic_repair']['changes'];comp=a['comparison']['tier_evidence'];fails=sorted({r['tag'] for r in comp.get('vetoes',[])})
  diagnosis_path=ap.parent/('diagnosis-'+str(a['number'])+'.json');comparison_path=ap.parent/('comparison-'+str(a['number'])+'.json')
  lines.append(f"- 第 {a['number']} 次：IPAdapter 权重 {a['image']['reference']['strength']}，来源服装加权 {ch['sourced_outfit_reinforcement'] or '无'}，seed {a['image']['seed']}，prompt_id `{a['image']['prompt_id']}`；{('fail: '+', '.join(fails)) if fails else '全部可靠适用项通过，无 fail veto'}。诊断和变化在 `{diagnosis_path}`，原比较 `{comparison_path}`。\n")
 lines.append(f"- 通过定妆：`{job/portrait['image']['path']}`；最终 gate `{job/'l3-final-gate.json'}`；D5 身份面部参考 `{job/portrait['face']['path']}`，记录 `{job/'portrait.json'}`。后续镜头仍使用已审核定妆面部和 IPAdapter 0.4，离线正式流程测试覆盖 none/qwen 路径。\n")
 lines.append(f"- 三栏图：`review/P6-02-L3-{d['character_tag'].replace('/','_')}.jpg`（左知识探针／中严格排名参考／右通过的定妆）。\n")
lines+=['\n0.55 高于后续故事的 0.4，是知识探针否决后用更强面部条件恢复来源身份的诊断假设。第一候选首次通过，无重试；第二候选首次只因 torn_shirt 失败，第二次保持面部强度 0.55、给该来源服装 tag 1.2 权重，随后通过。没有 seed-only 重试，也没有换参考或挑别的 seed。\n',
'### L4 与资源检查实测\n',
'- 随机中文名 `岚砚柚澄霁珞`：本轮 Danbooru 无候选，中文维基精确搜索 totalhits=0、标题 missing。初始 403 不当查不到；带项目 User-Agent 后 HTTP 200 的响应另存 `runs/p6_02_rework1/l4/unknown/chinese-wikipedia-*-identified.json`。`needs-user.json` 原因是“找不到该角色的资料”，提供参考图路径、角色 tag、作品名的补充途径。\n',
'- `frederica_irving_(sekaiju)`：有角色 tag，严格可用参考不足，返回“底模身份未验证且没有足够的可靠参考图”；见 `runs/p6_02_rework1/l4/insufficient/needs-user.json` 及参考筛选 manifest。两例均无 state.json、无生成图；正式 pipeline 提交边界复跑零 submit 调用，`l4-formal-replay.json`。本轮提交记录只包括 11 个知识探针和 3 次 L3 定妆，无 L4 用例提交。\n',
'- 轻量资源检查的请求、字节数、逐项耗时在 `resources-live.json`（最终含 LoadImage 检查）；超时为配置 180 秒。不把共享负载下的等待当作吞吐测评；初测 `/system_stats` 本身等约 36 秒，旧 10 秒超时依然不够。\n',
'### 用量、产出与限制\n',
f'- 本轮真实 ComfyUI 提交/出图 **{count}/18**：11 个知识探针、3 次 L3 定妆；剧情图 0，两个 L4 用例 0。视觉请求 **{u["vision_requests"]}**，加本地看三栏图 **{sum(r["count"] for r in views)}** 次，共 **{visual}/60**。原轮统计保留在前文，不混作本轮用量。\n',
f'- 实测脚本 {started.astimezone().isoformat()} 至 {ended.astimezone().isoformat()}，{(ended-started).total_seconds()/60:.1f} 分钟；本轮从 08:56 执行记录至报告写入约 {(now-datetime(2026,10,1,0,56,7,tzinfo=timezone.utc)).total_seconds()/60:.1f} 分钟（预算 90）。可能与 Hermes 剧情任务共享 ComfyUI/视觉服务，耗时包含并发等待；没有清队列、interrupt 或跟踪他人的任务。\n',
'- 汇总/完整证据：`reports/assets/P6-02-rework1/{usage-final,artifact-verification,collages}.json`、自身 prompt history、每任务 identity/knowledge/portrait、references manifest、workflow 与诊断。其余角色的新探针对照为 `review/P6-02-rework1-probe-<tag>.jpg`，未覆盖原轮旧图。\n',
'- 自动通过只代表 D8 的可靠确认/失败否决门禁；瞳色、配饰、整体角色等仍未升级为可靠确认。肉眼可见 hime 的新定妆恢复双马尾/皇冠、festa 恢复帽子/外套形态；细节纹样、比例与参考不是逐像素一致，最终可识别性由监工看图验收。两个新建 tag 都关联既有母角色/形态，知识探针仍画出相近发色或马耳，不能把创建日期或 L2 否决扩大为“模型从未见过母角色”的证明。\n',
'- 其余否决角色仅为待 L3，未声称其定妆成功，也未生成冒充该角色的剧情图；该选择遵守“只给最先 1–2 个做完整链”的本轮范围。未修改禁写文件、已有作品或 ComfyUI 的节点/模型/配置，未 commit。项目内指定 SKILL 已改，安装由监工执行。\n']
fragment=''.join(lines)
fragment=re.sub(r'(^#{2,3} .+)\n',r'\1\n\n',fragment,flags=re.M)
fragment=re.sub(r'(\*\*\n)(?=-)',r'\1\n',fragment)
fragment_path=assets/'report-fragment.md'
previous=fragment_path.read_text() if fragment_path.exists() else None
report=Path('reports/P6-02.md');text=report.read_text()
if '\n## 返工 1\n' in text:
 assert '--refresh' in sys.argv and previous and text.endswith(previous),'refuse to replace unknown/other report content'
 report.write_text(text[:-len(previous)]+fragment)
else:
 with report.open('a') as out:out.write(fragment)
fragment_path.write_text(fragment)
save_json_atomic(assets/'completion.json',{'at':now.isoformat(),'images':count,'vision_requests':u['vision_requests'],'local_views':len(views),'total_visual_calls':visual})
print('Appended 返工 1')
