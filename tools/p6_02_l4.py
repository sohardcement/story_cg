"""Run two real identity-negative cases; no generation API is called."""
import json,time
from pathlib import Path
import httpx
from storycg.pipeline import new_job,run_story_job
from storycg.config import load_config
from storycg.comfy import ComfyClient
from storycg.identity import resolve_identity
from storycg.state import save_json_atomic
root=Path('runs/p6_02/l4');cfg=load_config('config/default.json')
name='岚砚柚澄霁珞'
job=root/'unknown';new_job(job,name,'在窗边整理一本书。',generation_config=cfg)
identity=resolve_identity(name,job/'cache/danbooru');save_json_atomic(job/'identity.json',identity)
client=ComfyClient(cfg['comfyui_url'],180)
try:
 r=run_story_job(job,cfg,client);save_json_atomic(job/'needs-user.json',r);print('unknown',r['status'],flush=True)
 source=Path('runs/p6_02/candidates/frederica_irving_(sekaiju)')
 target=root/'insufficient';new_job(target,'frederica_irving_(sekaiju)','在窗边整理一本书。',generation_config=cfg)
 save_json_atomic(target/'identity.json',json.loads((source/'identity.json').read_text()))
 inv=json.loads(Path('reports/assets/P6-02/inventory.json').read_text())
 from storycg.tiers import ensure_identity_tier
 r=ensure_identity_tier(target,json.loads((target/'identity.json').read_text()),cfg,client,inventory=inv)
 save_json_atomic(target/'needs-user.json',r);print('insufficient',r['status'],r.get('reason'),flush=True)
finally:client.close()
with httpx.Client(timeout=30,trust_env=False) as c:
 url='https://zh.wikipedia.org/w/api.php';r=c.get(url,params={'action':'query','list':'search','srsearch':name,'format':'json'})
 save_json_atomic(job/'chinese-wikipedia.json',{'url':str(r.url),'status':r.status_code,'response':r.json() if r.status_code==200 else r.text})
 print('zhwiki',r.status_code,flush=True)
