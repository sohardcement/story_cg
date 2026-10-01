"""Package already generated artifacts and append the rework report, local only."""
import json,hashlib,shutil,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'calibration/v2/samples';E=ROOT/'reports/assets/P9-03/r1'
def load(p):return json.loads(p.read_text())
def save(p,j):p.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n')
observations=load(E/'observations.json')
for sid in ['B2','B3']:
 r=load(S/(sid+'.json'));obs=observations[sid];at=load(E/f'{sid}-attempt{obs["final_attempt"]}.json');assert at['status']=='completed'
 dest=S/(sid+'.png');assert not dest.exists();shutil.copyfile(ROOT/at['image_path'],dest)
 r.update(method='qwen_edit',status='prepared',source_role='actual_Qwen_image1_input',claim='重复主角 = error',claimed_truth={'statement':'重复主角 = error','confirmed':False,'scope':'protagonist_count'},image_path=str(dest.relative_to(ROOT)),image_sha256=at['image_sha256'],edit_effective=obs['duplicate_added'],executor_observation=obs['observation'],uncertainties=obs['uncertainties'],edit_instruction=at['instruction'],prompt_id=at['prompt_id'],workflow=at['workflow'],comfy_output=at['comfy_output'],retries=obs['final_attempt']-1,final_attempt=obs['final_attempt'],attempt_records=[str((E/f'{sid}-attempt{i}.json').relative_to(ROOT)) for i in range(1,obs['final_attempt']+1)],supervisor_confirmed=False,edit_constraints_satisfied=obs['same_outfit'])
 save(S/(sid+'.json'),r)
m=load(ROOT/'calibration/v2/manifest.json');m['samples']=[load(S/(sid+'.json')) for sid in m['sample_ids']];m['missing_images']=[];u=load(E/'usage.json');u['elapsed_seconds_to_packaging']=round(time.time()-u['started_epoch'],2);save(E/'usage.json',u)
m.update(generated_images=u['generated_images'],comfyui_submissions=u['submissions'],review_calls=0,vlm_calls=0,rework_round=1,rework_usage=u,supervisor_confirmation_required=True);save(ROOT/'calibration/v2/manifest.json',m)
u0=load(ROOT/'calibration/v2/usage.json');u0['initial_round']=dict(u0);u0.update(generated_images=u['generated_images'],comfyui_submissions=u['submissions'],copied_images=25,metadata_records=27,rework_1=u);save(ROOT/'calibration/v2/usage.json',u0)
labels=ROOT/'verify/P9-labels.json';save(E/'labels_readonly_snapshot_hash.json',{'path':str(labels.relative_to(ROOT)),'sha256':hashlib.sha256(labels.read_bytes()).hexdigest(),'used_for':'只读参考监工裁定，不修改或重判标签'})
report=ROOT/'reports/P9-03.md';lines=['','## 返工 1','','本节为最新交付状态；上文的 24 PNG、A6/B2/B3 缺口与远端禁令冲突描述是首轮历史，已由本次监工澄清及指定来源解除。当前为 **27 PNG + 27 JSON**。','','### 做了什么与验证','','- B2 首次 Qwen 编辑完成；B3 首次编辑存在服装漂移，记录诊断后仅重做一次。使用现有 Qwen Image Edit GGUF + Lightning 工作流，无 Qwen-Image 2.1/ControlNet；通过 ComfyUI API 串行上传与提交，没有安装依赖、改配置/节点/模型。','','- A6 复制监工指定约尔背影，来源与 A2 相同；身份/服装/原分镜同步改为该来源任务，瞳色不可见，不再沿用旧芙莉莲元数据。','','- A1/A2 按只读 `verify/P9-labels.json` 记录监工裁定并清除当前疑问，保留执行者此前疑问的历史字段；未修改监工真值。A6/B2/B3 仍待监工复核。','','- 已更新 `calibration/v2/samples/{A1,A2,A6,B2,B3}.json`、新增 `{A6,B2,B3}.png`，更新 `calibration/v2/manifest.json`、`usage.json`、`review/P9-03-samples.jpg` 与本报告；新增 `tools/p9_03_r1_edit.py`、`p9_03_r1_finish.py`、`p9_03_render.py`，更新只读完整性校验 `tools/p9_03_verify.py`。串行生成日志、工作流、真实 prompt_id/history、所有新编辑图片与自行判断保存在 `reports/assets/P9-03/r1/`。','','可直接复制运行（仅本地读取，不调用生成/审核）：','','```bash','.venv/bin/python tools/p9_03_verify.py','```','','PNG 可解码与 sha256、复制图与原图一致、编辑图与真实 history 输出一致、编号/metadata/manifest 一致；没有检测、瞳色采样或检出率统计。','','### 新增样本来源与自行看图判断','']
for sid in ['A6','B2','B3']:
 r=load(S/(sid+'.json'));lines.extend([f'- **{sid}** 来源 `{r["source_image"]}`，sha256 `{r["source_sha256"]}`；输出 `{r["image_path"]}`。做法 `{r["method"]}`；声称 `{r["claim"]}`。自行观察：{r["executor_observation"]}', ''])
lines.extend(['### 诊断与全部尝试','','B3 首次编辑诊断保存在 B3.json 的 retry_diagnosis：第二人领口闭合，与原图横条开口领不同，新增黑色手持物；假设身份/服装 tag 扩写使模型自行设计。重做保留 seed、仍用原始单人源图，强化视觉服装细节一致、禁止新道具；不是只换 seed 或同义词。'])
for sid in ['B2','B3']:
 for i in range(1,observations[sid]['final_attempt']+1):
  at=load(E/f'{sid}-attempt{i}.json');lines.extend(['',f'- {sid} attempt {i}：prompt_id `{at["prompt_id"]}`；耗时 {at["elapsed_seconds"]} 秒；输出 `{at["image_path"]}`，sha256 `{at["image_sha256"]}`。'])
lines.extend(['','### 用量与已知问题','',f'返工 1 新出图 {u["generated_images"]}/4，API 提交 {u["submissions"]}，审核/视觉模型/检测/颜色采样调用全部 0；从首次出图准备至打包 {u["elapsed_seconds_to_packaging"]} 秒（前置阅读另计），低于 30 分钟。没有 git commit；没有修改 verify 或其它禁止文件。','','新增三样本的真值均未经监工复核，不能直接进入 P9-04 统计。自行判断仅说明图片观测，不构成审核器结论。'])
for sid in ['B2','B3']:
 lines.extend(['',f'- {sid} 已知限制：'+('；'.join(observations[sid]['uncertainties']) or '无新增阻塞；仍需监工确认真值。')])
report.write_text(report.read_text()+'\n'.join(lines)+'\n')
print('Packaged',len(m['samples']),'samples; generated',u['generated_images'])
