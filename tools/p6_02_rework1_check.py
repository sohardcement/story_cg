"""Read-only integrity checks plus final D8 aggregation of raw observations.

No image generation, no visual call, and no changes to any observed status.
"""
import json,hashlib
from pathlib import Path
from storycg.config import load_config
from storycg.comfy import ComfyClient
from storycg.state import save_json_atomic
from storycg.tiers import evaluate_l2,identity_checklist,knowledge_probe_tags
from storycg.identity_enrichment import load_rules
assets=Path('reports/assets/P6-02-rework1');ledger=json.loads((assets/'usage.json').read_text())
reliability=json.loads(Path('config/review_reliability.json').read_text());cfg=load_config('config/default.json');checks={'roles':[],'prompts':[],'l4':[],'frozen':[]}
ids={r['prompt_id'] for r in ledger['submissions'] if r.get('prompt_id')};histories={}
assert len(ledger['submissions'])<=18 and ledger['vision_requests']<=56
client=ComfyClient(cfg['comfyui_url'],cfg['http_timeout_seconds'])
try:
 for item in ledger['submissions']:
  pid=item['prompt_id'];record=client.get_history(pid).get(pid)
  assert record and record['status']['completed'] and record['status']['status_str']=='success'
  assert record['prompt'][2]['9']['inputs']['filename_prefix']==item['prefix']
  histories[pid]=record
  save_json_atomic(assets/'prompt-history'/f'{pid}.json',record)
  checks['prompts'].append({'prompt_id':pid,'prefix':item['prefix'],'status':'success'})
finally:client.close()
for row in ledger['results']:
 job=Path(row['job']);original=json.loads((job/'original-identity.json').read_text());identity=json.loads((job/'identity.json').read_text());knowledge=json.loads((job/'knowledge-result.json').read_text());e=knowledge['tier_evidence']
 for key in ('character_tag','work_tag','identity_block','outfit_block','appearance_tags','default_outfit_tags','default_outfit_selection'):
  assert identity.get(key)==original.get(key),(row['tag'],key)
 probe=job/e['artifact_root'];test=e['test_images'][0];graph=json.loads((probe/test['workflow_path']).read_text())
 assert test['prompt_id'] in ids
 assert graph==histories[test['prompt_id']]['prompt'][2]
 expected=cfg['positive_prefix']+', '+knowledge_probe_tags(identity)
 lora=knowledge.get('lora')
 if lora and lora.get('trigger_words'):expected+=', '+', '.join(lora['trigger_words'])
 assert graph['4']['inputs']['text']==expected,(row['tag'],graph['4']['inputs']['text'])
 assert graph['7']['inputs']['denoise']==1 and graph['7']['inputs']['latent_image']==['6',0]
 assert not any(n['class_type'] in ('LoadImage','IPAdapterAdvanced','VAEEncode','TextEncodeQwenImageEditPlus') for n in graph.values())
 expected_loras=[r['name'] for r in cfg['style_loras']]+([lora['name']] if lora else [])
 assert sorted(n['inputs']['lora_name'] for n in graph.values() if n['class_type']=='LoraLoader')==sorted(expected_loras)
 for ref in e['references']:
  if ref.get('status')=='downloaded':assert hashlib.sha256((probe/ref['path']).read_bytes()).hexdigest()==ref['sha256']
 raw=[item for pair in e['comparisons'] for item in pair['items']]
 final=evaluate_l2(identity_checklist(identity),raw,reliability)
 if lora and final['identity_tier']=='L2':final['identity_tier']='L1'
 final['tier_evidence']={**e,**{key:final[key] for key in ('reliable','unverified','outfit_comparisons','vetoes','not_visible','limitations')}}
 final['tier_evidence']['tier_scope']=load_rules()['tier_review']['tier_scope']
 final['raw_observations_unchanged']=True
 save_json_atomic(job/'knowledge-final.json',final)
 # The running process loaded the first draft of D8; retain its raw artifacts,
 # and make the final declared tier match all final reliable/applicable gates.
 row['probe_tier']=final['identity_tier'];row['vetoes']=sorted({x['tag'] for x in final['vetoes']})
 row['reliable_unconfirmed']=[x['tag'] for x in final['reliable'] if not x['passed']]+[x['tag'] for x in final['outfit_comparisons'] if x['applicable'] and x['reliability']=='reliable' and not x['passed']]
 if str(job) not in ledger['selected_l3']:
  identity['identity_tier']=final['identity_tier'];identity['tier_evidence']=final['tier_evidence'];save_json_atomic(job/'identity.json',identity)
  row['tier']=identity['identity_tier']
 else:
  if identity['identity_tier']=='L3':
   p=json.loads((job/'portrait.json').read_text());a=json.loads((job/identity['tier_evidence']['l3']['attempts_file']).read_text())[p['approved_attempt']-1]
   raw3=[item for pair in a['comparison']['tier_evidence']['comparisons'] for item in pair['items']]
   gate=evaluate_l2(identity_checklist(identity),raw3,reliability)
   assert gate['identity_tier']=='L2','L3 approval must satisfy final D8 gate'
   save_json_atomic(job/'l3-final-gate.json',gate)
   from storycg.reference import require_portrait
   from storycg.fallback import current_l3_approval
   assert current_l3_approval(p['approved_identity'])
   require_portrait(job)
 row['not_visible_count']=len(final['not_visible']);checks['roles'].append({'tag':row['tag'],'probe_tier':row['probe_tier'],'tier':row['tier'],'canonical_prompt':expected,'immutable_source':True})
for path in Path('runs/p6_02_rework1').rglob('state.json'):
 state=json.loads(path.read_text())
 for a in state['attempts']:
  assert a['prompt_id'] in ids
  assert a['workflow']==histories[a['prompt_id']]['prompt'][2]
  if a['status']=='completed':assert hashlib.sha256((path.parent/a['image']['path']).read_bytes()).hexdigest()==a['image']['sha256']
for job in Path('runs/p6_02_rework1/l4').iterdir():
 d=json.loads((job/'identity.json').read_text());assert d['status']=='needs_user' and d['identity_tier']=='L4'
 assert not list(job.rglob('state.json')) and not list(job.rglob('*.png'))
 result=json.loads((job/'needs-user.json').read_text());checks['l4'].append({'job':str(job),'reason':d['reason'],'state_files':[],'comfy_submissions':0})
for name,item in json.loads(Path('reports/assets/P6-02/frozen-hashes.json').read_text()).items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==item['head'];checks['frozen'].append(name)
save_json_atomic(assets/'usage-final.json',ledger);save_json_atomic(assets/'artifact-verification.json',checks)
print(json.dumps({'roles':len(checks['roles']),'prompts':len(checks['prompts']),'l4':checks['l4']},ensure_ascii=False))
