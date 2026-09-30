import json,time
from pathlib import Path
import httpx
from storycg.tiers import lora_names
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-01b');root.mkdir(parents=True,exist_ok=True)
with httpx.Client(base_url='http://127.0.0.1:8188',timeout=180,trust_env=False) as client:
 response=client.get('/object_info');response.raise_for_status()
 save_json_atomic(root/'object_info-live.json',response.json())
 names=lora_names(response.json());metadata={}
 for number,name in enumerate(names):
  try:
   response=client.get('/view_metadata/loras',params={'filename':name});response.raise_for_status();metadata[name]=response.json()
  except Exception as exc:metadata[name]={'fetch_error':str(exc)}
  save_json_atomic(root/'lora-metadata-live.json',metadata)
  print(number+1,len(names),name,flush=True)
