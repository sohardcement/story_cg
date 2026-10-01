"""Read-only artifact checks, own prompt IDs only. Does not generate or review."""
import hashlib,json,subprocess
from pathlib import Path
from storycg.state import save_json_atomic
from storycg.comfy import ComfyClient
from storycg.config import load_config
root=Path('reports/assets/P6-02');cfg=load_config('config/default.json');ledger=json.loads((root/'live-usage.json').read_text())
checks={'prompt_ids':[],'pending':[],'references':[],'l4':[],'frozen':[]}
client=ComfyClient(cfg['comfyui_url'],180)
try:
 for submission in ledger['submissions']:
  pid=submission['prompt_id'];record=client.get_history(pid).get(pid)
  if not record:
   checks['pending'].append(pid);continue
  assert record['status']['completed'] and record['status']['status_str']=='success'
  workflow=record['prompt'][2];assert workflow['9']['inputs']['filename_prefix']==submission['prefix']
  checks['prompt_ids'].append({'prompt_id':pid,'prefix':submission['prefix'],'status':'success'})
finally:client.close()
for state_path in Path('runs/p6_02').rglob('state.json'):
 state=json.loads(state_path.read_text())
 for a in state['attempts']:
  assert a['prompt_id'] in {x['prompt_id'] for x in ledger['submissions']}
  if a['status']=='completed':
   image=state_path.parent/a['image']['path'];assert hashlib.sha256(image.read_bytes()).hexdigest()==a['image']['sha256']
for path in Path('runs/p6_02').rglob('manifest.json'):
 data=json.loads(path.read_text())
 if not isinstance(data,list):continue
 for ref in data:
  if ref.get('status')=='downloaded':
   candidates=[ancestor/ref['path'] for ancestor in path.resolve().parents if ancestor.is_relative_to(Path.cwd())]
   assert any(im.is_file() and hashlib.sha256(im.read_bytes()).hexdigest()==ref['sha256'] for im in candidates)
   checks['references'].append({'manifest':str(path),'post_id':ref['post_id'],'sha256':ref['sha256']})
for job in Path('runs/p6_02/l4').iterdir():
 identity=json.loads((job/'identity.json').read_text());assert identity['status']=='needs_user' and identity['identity_tier']=='L4'
 assert not list(job.rglob('state.json')) and not list(job.rglob('*.png'))
 checks['l4'].append({'job':str(job),'reason':identity['reason'],'generated_images':0})
for name,item in json.loads((root/'frozen-hashes.json').read_text()).items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==item['head'];checks['frozen'].append(name)
assert len(ledger['submissions'])<=10 and ledger['vision_requests']<=40
save_json_atomic(root/'artifact-verification.json',checks)
print(json.dumps({'verified_prompts':len(checks['prompt_ids']),'pending':checks['pending'],'verified_references':len(checks['references']),'l4':checks['l4']},ensure_ascii=False))
