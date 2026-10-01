"""D8 serial evidence run; original artworks remain untouched."""
import json,time,shutil,hashlib
from datetime import datetime,timezone
from pathlib import Path
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.tiers import ensure_identity_tier,lora_names
from storycg.fallback import ensure_l3,comparison_available
from storycg.review import VisionClient
from storycg.state import save_json_atomic
assets=Path('reports/assets/P6-02-rework1');assets.mkdir(parents=True,exist_ok=True)
ledger_path=assets/'usage.json'
ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {'started_at':datetime.now(timezone.utc).isoformat(),'submissions':[],'vision_requests':0,'results':[],'selected_l3':[]}
deadline=datetime.fromisoformat(ledger['started_at']).timestamp()+90*60
cfg=load_config('config/default.json')
def save():save_json_atomic(ledger_path,ledger)
def within_budget():
 if time.time()>=deadline:raise ValueError('P6-02 rework1 90 minute wall-clock budget exhausted')
class Client(ComfyClient):
 def submit_prompt(self,wf,cid):
  within_budget()
  if len(ledger['submissions'])>=18:raise ValueError('P6-02 rework1 image budget exhausted')
  entry={'prefix':wf['9']['inputs']['filename_prefix'],'status':'reserved','at':datetime.now(timezone.utc).isoformat()}
  ledger['submissions'].append(entry);save()
  pid=super().submit_prompt(wf,cid);entry.update(prompt_id=pid,status='submitted');save();return pid
class Vision(VisionClient):
 def call(self,*args,**kwargs):
  within_budget()
  if ledger['vision_requests']>=56:raise ValueError('P6-02 rework1 visual budget reserve reached')
  ledger['vision_requests']+=1;save()
  return super().call(*args,**kwargs)
client=Client(cfg['comfyui_url'],cfg['http_timeout_seconds']);save()
try:
 inv=json.loads(Path('reports/assets/P6-02/inventory.json').read_text())
 response=client.http.get('/object_info/LoraLoader');response.raise_for_status();current=lora_names(response.json())
 save_json_atomic(assets/'lora-node-live.json',response.json())
 if sorted(current)!=sorted(inv['names']):raise ValueError('LoRA inventory changed; rerun inventory before reassessment')
 inv['inventory_rechecked_at']=datetime.now(timezone.utc).isoformat();inv['object_info_url']=cfg['comfyui_url']+'/object_info/LoraLoader'
 inv['metadata_provenance']='P6-02 inventory, model list rechecked live unchanged; source cache retained'
 save_json_atomic(assets/'inventory.json',inv)
 rows=json.loads(Path('reports/assets/P6-02/usable-candidates.json').read_text())
 previous=json.loads(Path('reports/assets/P6-02/live-usage.json').read_text())
 source_jobs={r['tag']:r['job'] for r in previous['results']}
 for row in rows:
  row['source_job']=source_jobs.get(row['tag'],row['job'])
 for number in (3,4,1,2):
  source=Path('runs/p6_01b')/f'p6_01b_{number:02d}';d=json.loads((source/'identity.json').read_text())
  rows.append({'tag':d['character_tag'],'source_job':str(source),'existing_role':True})
 save_json_atomic(assets/'ordered-roles.json',rows)
 for index,row in enumerate(rows,1):
  if any(r['index']==index for r in ledger['results']):
   print('CACHED',index,row['tag'],flush=True);continue
  within_budget()
  job=Path('runs/p6_02_rework1')/f'p6_02_r1_{index:02d}';source=Path(row['source_job'])
  if not (job/'identity.json').exists():
   original=json.loads((source/'identity.json').read_text());save_json_atomic(job/'original-identity.json',original)
   identity=json.loads(json.dumps(original));identity['identity_tier']='tier_pending_L2';identity['tier_evidence']={'status':'D8 new knowledge probe','original_job':str(source),'original_tier':original['identity_tier']}
   save_json_atomic(job/'identity.json',identity)
   source_refs=source/original['tier_evidence']['artifact_root']/'references_v2'
   shutil.copytree(source_refs,job/'tier'/('probe_v3_'+job.name)/'references_v2',dirs_exist_ok=True)
  print('START',index,row['tag'],flush=True);start=time.monotonic()
  vision=Vision(job.resolve(),cfg['model_service_url'],max_calls=24,max_seconds=2400)
  try:
   identity=json.loads((job/('knowledge-result.json' if (job/'knowledge-result.json').exists() else 'identity.json')).read_text())
   result=ensure_identity_tier(job,identity,cfg,client,inventory=inv,vlm=vision,allow_l3=False)
   probe_tier=result['identity_tier'];save_json_atomic(job/'knowledge-result.json',result)
   if (index<=7 and probe_tier=='tier_pending_L3_L4' and comparison_available({'tier_evidence':result['tier_evidence']})
       and (str(job) in ledger['selected_l3'] or len(ledger['selected_l3'])<2)):
    if str(job) not in ledger['selected_l3']:ledger['selected_l3'].append(str(job));save()
    probe=job/result['tier_evidence']['artifact_root'];refs=json.loads((probe/'references_v2/manifest.json').read_text())
    result=ensure_l3(job,result,cfg,client,references=refs,reference_root=probe,vlm=vision)
   entry={'index':index,'tag':row['tag'],'job':str(job),'old_tier':json.loads((job/'original-identity.json').read_text())['identity_tier'],
          'probe_tier':probe_tier,'tier':result['identity_tier'],'reason':result.get('reason'),'elapsed':time.monotonic()-start,
          'vetoes':sorted({x['tag'] for x in result['tier_evidence'].get('vetoes',[])})}
   ledger['results']=[r for r in ledger['results'] if r['index']!=index]+[entry];save();print('RESULT',entry,flush=True)
  finally:vision.close()
finally:
 ledger['ended_at']=datetime.now(timezone.utc).isoformat();save();client.close()
