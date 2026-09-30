"""Serial calibration producer; all specific instructions live in persisted sample specs."""
import argparse, copy, hashlib, json, time, shutil
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw, ImageFont
from storycg.comfy import ComfyClient
from storycg.workflow import build_workflow, build_qwen_workflow
from storycg.assembly import assemble_prompt
ROOT=Path('calibration'); S=ROOT/'samples'
def load(p):return json.loads(Path(p).read_text())
def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def produce():
 spec=load(ROOT/'spec.json'); start=time.time(); deadline=start+3500
 client=ComfyClient(spec['comfyui_url'],180)
 stats=load(ROOT/'usage.json') if (ROOT/'usage.json').exists() else {'submissions':0,'generated_images':0,'review_calls':0,'vlm_calls':0,'started_epoch':start}
 deadline=stats['started_epoch']+3500
 try:
  r=client.http.get('/system_stats');r.raise_for_status();save(ROOT/'system_stats.json',r.json())
  for item in spec['samples']:
   sid=item['id']; jp=S/f'{sid}.json'; out=S/f'{sid}.png'
   if out.exists():continue
   if jp.exists() and load(jp).get('status') in ('submitting','running'):
    raise RuntimeError(f'{sid}: prior submission unresolved; inspect saved prompt_id/history, do not resubmit')
   record=copy.deepcopy(item);record.update(edit_effective=None,supervisor_confirmed=False)
   if item['method']=='copy':
    shutil.copyfile(item['source_image'],out);record['image_sha256']=sha(out);save(jp,record);continue
   if time.time()>deadline:raise RuntimeError('time budget exhausted')
   if stats['submissions']>=28:raise RuntimeError('submission budget exhausted')
   cfg=load(item['config_source']);cfg=cfg['config']
   if item['method']=='qwen_edit':
    upload=client.upload_image(item['source_image'],subfolder='p4_01_calibration');name='/'.join(filter(None,[upload.get('subfolder'),upload['name']]))
    with Image.open(item['source_image']) as im:w,h=im.size
    wf=build_qwen_workflow(cfg,name,item['edit_instruction'],w,h,item['seed'],f'storycg_P4_01_{sid}')
   else:
    identity=load(item['generation_identity']);entry=copy.deepcopy(item['generation_entry']);props=item['generation_props']
    if item.get('remove_named_tags'):
     identity['identity_block']=', '.join(['1girl']+[t['tag'] for t in identity['appearance_tags']]);identity['character_tag']='';identity['work_tag']=''
    if item.get('reference_image'):
     u=client.upload_image(item['reference_image'],subfolder='p4_01_calibration')
     cfg['generation_reference']={'mechanism':'ipadapter','strength':0.4,'uploaded_image':'/'.join(filter(None,[u.get('subfolder'),u['name']]))}
    else:cfg.pop('generation_reference',None)
    parts=assemble_prompt(identity,entry,props=props,config=cfg);cfg['negative_prompt']+=', '+parts['negative_tags']
    wf=build_workflow(cfg,parts['positive_tags'],parts['framing'],item['seed'],f'storycg_P4_01_{sid}')
   record['workflow']=wf;record['status']='submitting';record['submitted_epoch']=time.time();save(jp,record)
   stats['submissions']+=1;save(ROOT/'usage.json',stats)
   pid=client.submit_prompt(wf,'P4-01-calibration');record.update(prompt_id=pid,status='running');save(jp,record)
   print(sid,'submitted',pid,flush=True)
   history=client.wait_for_history(pid,2,max(1,deadline-time.time()));save(ROOT/f'{sid}-history.json',history)
   if not history:raise RuntimeError(f'{sid}: timed out; do not resubmit')
   if history.get('status',{}).get('status_str')=='error':raise RuntimeError(f'{sid}: workflow failed {history.get("status")}')
   imgs=history.get('outputs',{}).get('9',{}).get('images',[])
   if len(imgs)!=1:raise RuntimeError(f'{sid}: expected exactly one image')
   out.write_bytes(client.download_image(imgs[0]));record.update(status='completed',image_sha256=sha(out),comfy_output=imgs[0],elapsed_seconds=round(time.time()-record['submitted_epoch'],2));save(jp,record)
   stats['generated_images']+=1;stats['elapsed_seconds']=round(time.time()-stats['started_epoch'],2);save(ROOT/'usage.json',stats)
   print(sid,'completed',record['elapsed_seconds'],flush=True)
 finally:client.close()
def grid():
 spec=load(ROOT/'spec.json'); font=ImageFont.truetype('/usr/share/fonts/liberation/LiberationSans-Regular.ttf',20)
 w,h=380,470;sheet=Image.new('RGB',(w*5,h*7),'#eeeeee');d=ImageDraw.Draw(sheet)
 for row in range(7):
  c='A' if row<4 else 'B';ref=spec['references'][c]; im=ImageOps.contain(Image.open(ref).convert('RGB'),(w-12,h-65));sheet.paste(im,((w-im.width)//2,row*h+60));d.text((8,row*h+8),f'{c} approved reference',font=font,fill='black')
 # Arrange A and B in separate three-row groups; four rows for A, three for B.
 for c,base in [('A',0),('B',4)]:
  items=[x for x in spec['samples'] if x['character_group']==c]
  for n,item in enumerate(items):
   row=base+n//4;col=1+n%4;x,y=col*w,row*h
   d.text((x+8,y+8),item['id'],font=font,fill='black');d.text((x+8,y+32),item['claim'],font=font,fill='black')
   path=S/f"{item['id']}.png"
   if path.exists():
    im=ImageOps.contain(Image.open(path).convert('RGB'),(w-12,h-70));sheet.paste(im,(x+(w-im.width)//2,y+65))
 sheet.save('review/P4-samples.jpg',quality=95,subsampling=0)
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('action',choices=['produce','grid']);args=ap.parse_args();globals()[args.action]()
