"""Re-select references and re-review persisted probes; never submits generation."""
import json,time
from datetime import datetime,timezone
from pathlib import Path
import httpx
from storycg.config import load_config
from storycg.identity import BASE_URL,USER_AGENT
from storycg.identity_enrichment import load_rules
from storycg.review import VisionClient
from storycg.tiers import download_references,compare_l2
from storycg.state import save_json_atomic
assets=Path('reports/assets/P6-01b-rework1');assets.mkdir(parents=True,exist_ok=True)
config=load_config('config/default.json');rules=load_rules();start=time.monotonic()
progress_path=assets/'usage.json'
progress=json.loads(progress_path.read_text()) if progress_path.exists() else {
    'started_at':datetime.now(timezone.utc).isoformat(),'new_images':0,'vision_calls':0,'completed':[]}
with httpx.Client(base_url=BASE_URL,timeout=30,trust_env=False,headers={'User-Agent':USER_AGENT}) as client:
 for identity_path in sorted(Path('runs/p6_01b').glob('*/identity.json')):
  name=identity_path.parent.name
  if name in progress['completed']:continue
  if progress['vision_calls']+3>30:raise ValueError('rework vision budget insufficient')
  identity=json.loads(identity_path.read_text());evidence=identity['tier_evidence']
  snapshot=assets/name;snapshot.mkdir(exist_ok=True)
  if not (snapshot/'identity-before.json').exists():
   (snapshot/'identity-before.json').write_bytes(identity_path.read_bytes())
   collage=Path('review')/f"P6-01b-{identity['input_name']}.jpg"
   (snapshot/'collage-before.jpg').write_bytes(collage.read_bytes())
  probe=(identity_path.parent/evidence['artifact_root']).resolve()
  print('START',identity['input_name'],flush=True)
  references=download_references(identity['character_tag'],probe,client,rules=rules,identity=identity)
  if len([r for r in references if r['status']=='downloaded'])<2:raise ValueError('fewer than two filtered references')
  remaining=30-progress['vision_calls']
  vision=VisionClient(probe,config['model_service_url'],max_calls=min(remaining,6),max_seconds=600)
  try:
   result=compare_l2(probe,identity,evidence['test_images'],references,
          json.loads(Path('config/review_reliability.json').read_text()),vision,character_lora=identity.get('lora'))
  finally:
   progress['vision_calls']+=vision.calls;vision.close()
   progress['elapsed_seconds']=time.monotonic()-start;save_json_atomic(progress_path,progress)
  identity['identity_tier']=result['identity_tier']
  identity['tier_evidence']={**evidence,**result['tier_evidence'],'validation_completed':True,
      'previous_evidence_path':str(snapshot/'identity-before.json'),'rework':1,
      'reused_test_images':True,'rework_generation_count':0,
      'reference_selection_path':str(probe/rules['references']['directory']/'selection.json')}
  save_json_atomic(identity_path,identity)
  progress['completed'].append(name);save_json_atomic(progress_path,progress)
  print('RESULT',identity['input_name'],identity['identity_tier'],[r['post_id'] for r in references],vision.calls,flush=True)
print('USAGE',progress,flush=True)
