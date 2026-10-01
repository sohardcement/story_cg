"""Serial, budget-limited real tier runs. Never clears or interrupts shared queues."""
import json,time,shutil
from datetime import datetime,timezone
from pathlib import Path
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.tiers import ensure_identity_tier
from storycg.review import VisionClient
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-02');jobs=Path('runs/p6_02/candidates');cfg=load_config('config/default.json')
ledger_path=root/'live-usage.json';ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {'started_at':datetime.now(timezone.utc).isoformat(),'submissions':[],'vision_requests':0,'results':[]}
def save():save_json_atomic(ledger_path,ledger)
class Client(ComfyClient):
 def submit_prompt(self,wf,cid):
  if len(ledger['submissions'])>=10:raise ValueError('P6-02 image budget exhausted')
  entry={'prefix':wf['9']['inputs']['filename_prefix'],'status':'reserved','at':datetime.now(timezone.utc).isoformat()}
  ledger['submissions'].append(entry);save()
  pid=super().submit_prompt(wf,cid);entry.update(prompt_id=pid,status='submitted');save();return pid
class Vision(VisionClient):
 def call(self,*args,**kwargs):
  if ledger['vision_requests']>=36:raise ValueError('P6-02 visual budget reserve reached')
  ledger['vision_requests']+=1;save()
  return super().call(*args,**kwargs)
inv=json.loads((root/'inventory.json').read_text());client=Client(cfg['comfyui_url'],180)
try:
 candidates=json.loads((root/'usable-candidates.json').read_text())
 for index,row in enumerate(candidates,1):
  source=Path(row['job'])
  if index==5:
   job=source
  else:
   job=Path('runs/p6_02/live')/f'p6_02_{index:02d}'
   if not (job/'identity.json').exists():
    identity=json.loads((source/'identity.json').read_text())
    identity['identity_tier']='tier_pending_L2';identity['tier_evidence']={'status':'not_run','previous_transport_error':identity.get('tier_evidence',{}).get('error')}
    save_json_atomic(job/'identity.json',identity)
    source_refs=source/'tier'/('probe_'+source.name)/'references_v2'
    shutil.copytree(source_refs,job/'tier'/('probe_'+job.name)/'references_v2',dirs_exist_ok=True)
  identity=json.loads((job/'identity.json').read_text());print('START',row['tag'],flush=True);start=time.monotonic()
  vlm=Vision(job.resolve(),cfg['model_service_url'],max_calls=24,max_seconds=900)
  try:result=ensure_identity_tier(job,identity,cfg,client,inventory=inv,vlm=vlm)
  finally:vlm.close()
  entry={'tag':row['tag'],'job':str(job),'tier':result['identity_tier'],'reason':result.get('reason'),'elapsed':time.monotonic()-start}
  ledger['results'].append(entry);save();print('RESULT',entry,flush=True)
  if result['identity_tier'] in ('L3','L4'):break
finally:
 ledger['ended_at']=datetime.now(timezone.utc).isoformat();save();client.close()
