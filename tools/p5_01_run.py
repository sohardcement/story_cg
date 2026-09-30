"""P5-01 case driver. Case data is supplied as JSON, never embedded in primitives."""
import argparse,json,time
from pathlib import Path
from storycg.repairs import fork_repair_job,repair
from storycg.comfy import ComfyClient
from storycg.review import VisionClient,messages,BBOX_SCHEMA,normalize_bbox
from storycg.state import save_json_atomic,load_state

p=argparse.ArgumentParser();p.add_argument('cases');args=p.parse_args()
cases=json.loads(Path(args.cases).read_text())
for case in cases:
 root=Path(case['destination']).resolve();fork_repair_job(case['source'],root)
 state=load_state(root);config=state['config'];client=ComfyClient(config['comfyui_url'],180)
 started=time.monotonic()
 if case['primitive']=='inpaint' and not case.get('bbox'):
  source=next(a for a in state['attempts'] if a['shot_id']==f"shot{case['shot']:02d}")
  path=root/source['image']['path'];vlm=VisionClient(root,config['model_service_url'],max_calls=1)
  answer=vlm.call('repair_region',messages('Locate one rectangular edit region covering all objects and hand contacts relevant to this diagnosis, including intended target positions. Use normalized [left,top,right,bottom]. Diagnosis: '+case['diagnosis']['diagnosis']+' Intended shot: '+json.dumps(json.loads((root/'storyboard.json').read_text())['shots'][case['shot']-1]['shot']),[path]),BBOX_SCHEMA,[path]);vlm.close();save_json_atomic(root/'region_detection.json',answer)
  if answer['status']!='pass' or not answer['data']['found']:raise ValueError('no trustworthy repair region')
  bbox,_=normalize_bbox(answer['data']['bbox'],path);case['bbox']=bbox;case['region_source']={'file':'region_detection.json','call_id':answer['call_id']}
  save_json_atomic(Path(args.cases),cases)
 rec=repair(root,case['shot'],case['primitive'],case['diagnosis'],client,**{k:case[k] for k in ['bbox','region_source','prop_ids','denoise','seed','revised_entry','repair_id'] if k in case});client.close()
 save_json_atomic(root/'generation_timing.json',{'seconds':time.monotonic()-started});print(root,rec['output'],flush=True)
