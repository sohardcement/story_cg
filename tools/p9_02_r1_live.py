import json,time
from storycg.detect import ROOT
from storycg.repairs import fork_repair_job,repair
from storycg.comfy import ComfyClient
from storycg.detection_review import audit_detection
from storycg.state import save_json_atomic
from storycg.reference import digest
root=ROOT/'runs/p9_02_r1_duplicate';out=ROOT/'reports/assets/P9-02-r1';out.mkdir(exist_ok=True,parents=True)
fork_repair_job(ROOT/'jobs/cg-76d2eec57dfc',root)
board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text());entry=board['shots'][0];ledger={'images':0,'vision_calls':0,'results':[]}
class Limited(ComfyClient):
 def submit_prompt(self,workflow,client_id):
  if ledger['images']>=3:raise ValueError('image budget exhausted')
  ledger['images']+=1;save_json_atomic(out/'live.json',ledger)
  return super().submit_prompt(workflow,client_id)
client=Limited('http://127.0.0.1:8188',180)
try:
 for primitive in ['instruction_edit','relayout']:
  diagnosis={'diagnosis':'Local face/person detection reports duplicate protagonists.','hypothesis':'Retain the action-matching subject and restore background; detector count gates fallback.','changes':primitive,'result':{'status':'pending'}}
  kwargs={'source_attempt':1,'repair_id':'r1_'+primitive}
  if primitive=='instruction_edit':kwargs['edit']={'operation':'remove_duplicate_protagonist','target':'entry.shot.action','description':entry['shot']['action']}
  else:kwargs['revised_entry']=entry
  rec=repair(root,1,primitive,diagnosis,client,**kwargs)
  audit=audit_detection(root/rec['output']['path'],identity,entry)
  ledger['results'].append({'primitive':primitive,'output':rec['output'],'audit':audit});save_json_atomic(out/'live.json',ledger)
  print(primitive,rec['output'],audit['protagonist_count'],flush=True)
  if audit['protagonist_count']['status']!='duplicate':break
finally:client.close()
