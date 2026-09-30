"""Serial, state-derived P5-01b execution and recovery."""
import copy,json,time
from pathlib import Path
from PIL import Image
from storycg.repairs import fork_repair_job,repair,region_plan,region_queries
from storycg.comfy import ComfyClient
from storycg.review import VisionClient,messages,BBOX_SCHEMA,normalize_bbox
from storycg.state import save_json_atomic,load_state

asset=Path('reports/assets/P5-01b')
checkpoint=asset/'cases.json'
cases=json.loads(checkpoint.read_text() if checkpoint.exists() else Path('reports/assets/P5-01/cases.json').read_text())
if not checkpoint.exists():
 # Keep a separate copy of the pending job, retaining original prefix/workflow for recovery.
 for i,c in enumerate(cases):
  old=Path(c['destination']);new=Path(str(old).replace('p5_01_','p5_01b_'))
  c['previous']=str(old);c['destination']=str(new)
  if i==4:
   if not new.exists():
    import shutil
    shutil.copytree(old,new)
   c['repair_id']='repair2'
  else:fork_repair_job(c['source'],new)
  if i==0:c['operation']={'type':'remove','prop_ids':[]}
  if i==1:c['operation']={'type':'place','prop_ids':[]}
  if i==2:
   c['diagnosis']={'diagnosis':'Prior eye recoloring introduced warm colored spots inside the pupils.', 'hypothesis':'High classifier-free guidance amplifies warm accessory conditioning from the portrait in the enlarged small face.', 'changes':'Lower only WAI CFG from 5.5 to 3.5; keep reference strength, denoise, bbox and seed unchanged.', 'result':{'status':'pending'}}
   st=load_state(new);st['config']['repair_settings']['face_detail']['cfg']=3.5;save_json_atomic(new/'state.json',st)
  if i==3:c['reused_previous']=True
 save_json_atomic(asset/'cases.json',cases)
for i,c in enumerate(cases):
 root=Path(c['destination']);st=load_state(root)
 if i==3:continue
 board=json.loads((root/'storyboard.json').read_text());entry=board['shots'][c['shot']-1]
 if i<2 and c['primitive']=='inpaint':
  c['operation']['prop_ids']=[r['prop_id'] for r in entry['shot']['key_props'] if i==0 or r['held_by']=='none']
  detection=root/'region_detection_b.json'
  if detection.exists(): regions=json.loads(detection.read_text())
  else:
   source=next(a for a in st['attempts'] if a['shot_id']==f"shot{c['shot']:02d}");path=root/source['image']['path']
   vlm=VisionClient(root,st['config']['model_service_url'],max_calls=8);regions=[]
   for query in region_queries(entry,board['props'],c['operation']):
    q=query['query']+' Diagnosis: '+c['diagnosis']['diagnosis']+' Intended shot: '+json.dumps(entry['shot'])
    answer=vlm.call('repair_'+query['kind']+'_'+query['prop_id'],messages(q+' Return normalized [left,top,right,bottom].',[path]),BBOX_SCHEMA,[path]);regions.append({**query,'answer':answer})
   vlm.close();save_json_atomic(detection,regions)
  boxes=[];missing=[]
  for r in regions:
   a=r['answer']
   if a['status']=='pass' and a['data']['found']:
    path=root/next(a for a in st['attempts'] if a['shot_id']==f"shot{c['shot']:02d}")['image']['path'];boxes.append(normalize_bbox(a['data']['bbox'],path)[0])
   else:missing.append(r['kind'])
  if not boxes:raise ValueError('no localized objects')
  with Image.open(path) as im:
   settings=st['config']['repair_settings']['inpaint']
   plan=region_plan(boxes,im.size,settings['max_area_fraction'],settings['context_margin'])
  if 'target' in missing:plan['primitive']='relayout';plan['transfer_reason']='required target support not visible; local placement cannot be grounded'
  save_json_atomic(root/'region_plan.json',plan)
  c['bbox']=plan['bbox'];c['region_source']={'file':'region_detection_b.json','plan':plan}
  if plan['primitive']=='relayout':
   c['primitive']='relayout';c.pop('operation');c['diagnosis']['changes']='Transfer to single-shot relayout: '+plan['transfer_reason']
  else:c['diagnosis']['changes']='Union of diagnosed objects and intended target with context, aspect-preserving crop and feather mask; state-derived '+c['operation']['type']+' conditioning.'
 save_json_atomic(asset/'cases.json',cases)
 start=time.monotonic();client=ComfyClient(st['config']['comfyui_url'],180)
 try:
  rec=repair(root,c['shot'],c['primitive'],c['diagnosis'],client,**{k:c[k] for k in ['bbox','region_source','prop_ids','denoise','seed','revised_entry','repair_id','operation'] if k in c})
 finally:client.close()
 save_json_atomic(root/'generation_timing_b.json',{'seconds':time.monotonic()-start});print(root,rec['output'],flush=True)
