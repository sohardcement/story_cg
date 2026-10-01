"""Save factual final records and read-only remote submission provenance."""
import json,hashlib,sys
from pathlib import Path
import httpx
from storycg.budget import count_usage
root=Path(sys.argv[1]);dest=Path(sys.argv[2]);dest.mkdir(parents=True,exist_ok=True)
def read(name):
 p=root/name
 return json.loads(p.read_text()) if p.exists() else None
review=read('review.json') or {};cycle=read('cycle.json') or {}
result={'job':str(root),'usage':count_usage(root),'agent':read('agent.json'),
 'gate':read('review_gate.json'),'tier_gate':read('tier_gate.json'),
 'layer_conclusions':review.get('layer_conclusions'),'narrative_blind':review.get('narrative_blind'),
 'narrative_compare':review.get('narrative_compare'),'detection_items':review.get('detection_items'),
 'selected':{n:{'best_attempt':s.get('best_attempt'),'stop':s.get('stop'),'diagnosis_error':s.get('diagnosis_error'),
     'image':s.get('best_audit',{}).get('image')} for n,s in cycle.get('shots',{}).items()},
 'narrative_rejected':cycle.get('narrative_rejected'),'narrative_final_review_reused':cycle.get('narrative_final_review_reused')}
(dest/'final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
config=json.loads(Path('config/default.json').read_text());provenance=[]
with httpx.Client(base_url=config['comfyui_url'],timeout=8,trust_env=False) as client:
 for p in root.rglob('state.json'):
  state=json.loads(p.read_text())
  for a in state.get('attempts',[]):
   if a.get('inherited') or not a.get('post_count'):continue
   record={'state':str(p),'shot_id':a['shot_id'],'attempt':a['attempt_number'],'status':a['status'],
       'prompt_id':a.get('prompt_id'),'post_count':a.get('post_count'),'image':a.get('image')}
   im=a.get('image',{})
   if im.get('path'):
    path=p.parent/im['path'];record['local_hash_matches']=path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==im['sha256']
   if a.get('prompt_id'):
    try:
     response=client.get('/history/'+a['prompt_id']);response.raise_for_status();h=response.json().get(a['prompt_id'])
     record['remote_history_found']=bool(h)
     if h:
      record['remote_status']=h.get('status');record['remote_outputs']=h.get('outputs')
      raw=[x for v in h.get('outputs',{}).values() for x in v.get('images',[])]
      if im.get('comfy_output'):record['remote_filename_matches']=im['comfy_output'] in raw
      else:record['derived_image']='Local composite; remote raw generation is recorded by prompt_id, output filenames and repair stages.'
    except Exception as exc:record['history_error']=str(exc)
   provenance.append(record)
(dest/'provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2))
print(json.dumps({'usage':result['usage'],'layer_conclusions':result['layer_conclusions'],
 'generated_records':len(provenance),'hash_failures':sum(x.get('local_hash_matches') is False for x in provenance),
 'history_missing':sum(x.get('remote_history_found') is False for x in provenance)},ensure_ascii=False))
