import json,time
from pathlib import Path
import httpx
from storycg.config import load_config
from storycg.comfy import ComfyClient
from storycg.tiers import live_inventory,download_references
from storycg.identity import USER_AGENT
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-02');cfg=load_config('config/default.json')
with httpx.Client(base_url='https://danbooru.donmai.us',timeout=40,trust_env=False,headers={'User-Agent':USER_AGENT}) as ref:
 for job in sorted(Path('runs/p6_02/candidates').iterdir()):
  ident=json.loads((job/'identity.json').read_text())
  try:
   refs=download_references(ident['character_tag'],job/'tier'/('probe_'+job.name),ref,identity=ident)
   print(job.name,'hair',[x['tag'] for x in ident['appearance_tags'] if x['category']=='hair_color'],'references',len(refs),flush=True)
  except Exception as e:print(job.name,type(e).__name__,str(e),flush=True)
 client=ComfyClient(cfg['comfyui_url'],180)
 try:
  inv=live_inventory(cfg,client,ref,root);save_json_atomic(root/'inventory.json',inv)
  print('LIVE INVENTORY',len(inv['names']),flush=True)
 finally:client.close()
