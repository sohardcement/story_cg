"""Execute current order's probes serially; use this session's live inventory."""
import json,time
from datetime import datetime,timezone
from pathlib import Path
from storycg.state import save_json_atomic
from storycg.config import load_config
from storycg.comfy import ComfyClient
from storycg.tiers import ensure_identity_tier,lora_names
root=Path('reports/assets/P6-01b')
inventory={'names':lora_names(json.loads((root/'object_info-live.json').read_text())),
 'metadata':json.loads((root/'lora-metadata-live.json').read_text()),
 'character_tags':json.loads((root/'character-universe.json').read_text()),
 'object_info_url':'http://127.0.0.1:8188/object_info',
 'retrieved_at':datetime.fromtimestamp((root/'lora-metadata-live.json').stat().st_mtime,timezone.utc).isoformat()}
config=load_config('config/default.json');client=ComfyClient(config['comfyui_url'],180)
try:
 for index in range(1,5):
  job=Path('runs/p6_01b')/f'p6_01b_{index:02d}'
  if (job/'identity.json').exists():identity=json.loads((job/'identity.json').read_text())
  else:
   identity=json.loads((Path('runs/p6_01a_rework1_resolve')/f'{index:02d}'/'identity.json').read_text())
   save_json_atomic(job/'identity.json',identity)
  start=time.monotonic();print('START',identity['character_tag'],flush=True)
  result=ensure_identity_tier(job,identity,config,client,inventory=inventory)
  print('RESULT',result['character_tag'],result['identity_tier'],result['tier_evidence'].get('error'),time.monotonic()-start,flush=True)
  if result['identity_tier']=='tier_pending_resources':break
finally:client.close()
