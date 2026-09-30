"""Persist real frozen reviews plus separately supplied human observations."""
import json,time
from pathlib import Path
from storycg.repairs import record_result,write_repair_manifest
from storycg.state import load_state,save_json_atomic
from storycg.reference import digest
asset=Path('reports/assets/P5-01b');cases=json.loads((asset/'cases.json').read_text());observations=json.loads((asset/'observations.json').read_text());summary=[]
for c,observation in zip(cases,observations):
 root=Path(c['destination']);key=f"shot{c['shot']:02d}_{c.get('repair_id','repair1')}";name='repair_review_b_frozen.json' if c.get('reused_previous') else 'repair_review_frozen.json'
 record_result(root,key,{'file':name},observation)
 state=load_state(root);rec=state['repairs'][key];review=json.loads((root/name).read_text())
 logs=[json.loads(l) for l in (root/'review_log.jsonl').read_text().splitlines()]
 stages=[a for a in state['attempts'] if not a.get('inherited')]
 # The copied pending Qwen was submitted in the previous work order.
 fresh=[a for a in stages if a.get('completed_at') and a.get('post_count',0)]
 summary.append({'root':str(root),'primitive':c['primitive'],'output':rec['output'],'review':str(root/name),'review_conclusion':review['conclusion'],'human_observation':observation,'attempts':[{'prefix':a['filename_prefix'],'prompt_id':a.get('prompt_id'),'status':a['status'],'post_count':a.get('post_count',0),'cfg':a['workflow']['7']['inputs']['cfg']} for a in stages],'vision_requests':sum(l['event']=='request' for l in logs),'retained':c.get('reused_previous',False),'region_plan':rec.get('region_plan') or json.loads((root/'region_plan.json').read_text()) if (root/'region_plan.json').exists() else rec.get('region_plan')})
save_json_atomic(asset/'summary.json',{'cases':summary,'finished_at':time.time(),'composite_sha256':digest('review/P5-01b-repairs.jpg')})
print(json.dumps(summary,ensure_ascii=False,indent=2))
