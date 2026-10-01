"""Replay real unsupported inputs through the formal pipeline with a submit trap."""
import json
from pathlib import Path
from storycg.config import load_config
from storycg.pipeline import run_story_job
from storycg.state import save_json_atomic
class SubmitTrap:
 def __init__(self):self.calls=[]
 def submit_prompt(self,*args):
  self.calls.append(args)
  raise AssertionError('L4 must never submit a generated person')
root=Path('runs/p6_02/l4');checks=[]
for job in root.iterdir():
 client=SubmitTrap();result=run_story_job(job,load_config('config/default.json'),client)
 assert result['status']=='needs_user' and result['attempts']==[] and client.calls==[]
 assert not list(job.rglob('state.json'))
 save_json_atomic(job/'needs-user-replayed.json',result)
 checks.append({'job':str(job),'status':result['status'],'reason':result['reason'],'comfy_submission_attempts':len(client.calls),'state_files':[]})
save_json_atomic(Path('reports/assets/P6-02/l4-no-submissions.json'),checks)
print(json.dumps(checks,ensure_ascii=False))
