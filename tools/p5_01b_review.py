"""Review repaired shots and both actual neighboring images with frozen P4 entrypoints."""
import argparse,json,time
from pathlib import Path
from storycg.review import (VisionClient,audit_single,audit_consistency,identity_checklist,scene_checklist,messages,BBOX_SCHEMA,crop_face,face_pair,result_statuses,conclude)
from storycg.state import save_json_atomic,load_state
from storycg.reference import digest
from storycg.config import load_config

p=argparse.ArgumentParser();p.add_argument('cases');args=p.parse_args()
for case in json.loads(Path(args.cases).read_text()):
 root=Path(case['destination']).resolve();reportpath=root/'repair_review_b_frozen.json'
 if reportpath.exists(): print(root,'existing review; skipped',flush=True);continue
 state=load_state(root);config=load_config('config/default.json');rec=state['repairs'][f"shot{case['shot']:02d}_{case.get('repair_id','repair1')}"];board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text());portrait=json.loads((root/'portrait.json').read_text())
 vlm=VisionClient(root,config['model_service_url'],max_calls=8,max_seconds=600);artifacts=root/'repair_review_assets';artifacts.mkdir(exist_ok=True)
 if 'output' not in rec:
  print(root,'incomplete generation; review skipped',flush=True);vlm.close();continue
 path=root/rec['output']['path'];entry=rec['entry'];start=time.monotonic()
 report={'config':config,'image':rec['output'],'source':rec['source_image'],'single':audit_single(vlm,path,entry,board['props'],config,'repair_single')}
 detection=vlm.call('repair_face_bbox',messages('Locate main protagonist face; normalized [left,top,right,bottom] including forehead, eyes, chin and ears. If not visible found=false and bbox zeros.',[path]),BBOX_SCHEMA,[path]);report['face_detection']=detection
 compare=[root/portrait['image']['path'],path]
 if detection['status']=='pass' and detection['data']['found']:
  crop_face(path,detection['data']['bbox'],artifacts/'face.png');face_pair(root/portrait['face']['path'],artifacts/'face.png',artifacts/'face_pair.png');compare.append(artifacts/'face_pair.png')
 report['consistency']=audit_consistency(vlm,compare,identity_checklist(identity,entry['outfit']),config,'repair_identity')
 report['neighbors']=[]
 for num in [case['shot']-1,case['shot']+1]:
  if not 1<=num<=len(board['shots']):continue
  neighbor=next(a for a in reversed(state['attempts']) if a['shot_id']==f'shot{num:02d}' and a['status']=='completed');ne=board['shots'][num-1]
  checks={**identity_checklist(identity,entry['outfit']),**scene_checklist(entry['scene'],ne['scene'])}
  result=audit_consistency(vlm,[root/neighbor['image']['path'],path],checks,config,f'repair_neighbor_{num:02d}',reference_note=f'First image is actual adjacent shot {num}; second image repaired shot {case["shot"]}. Compare clothing and scene against both states. Adjacent outfit: '+json.dumps(ne['outfit']))
  report['neighbors'].append({'shot':num,'image':neighbor['image'],'review':result})
 vlm.close();report['conclusion']=conclude([*result_statuses(report['single']),*result_statuses(report['consistency']),*(s for n in report['neighbors'] for s in result_statuses(n['review']))]);report['usage']={'review_calls':vlm.calls,'seconds':time.monotonic()-start};save_json_atomic(reportpath,report);print(root,report['conclusion'],report['usage'],flush=True)
