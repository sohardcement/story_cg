"""Serial existing Qwen workflow edits only; no reviewer or detector."""
import argparse,json,time,hashlib
from pathlib import Path
from PIL import Image
from storycg.comfy import ComfyClient
from storycg.workflow import build_qwen_workflow
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'calibration/v2/samples';E=ROOT/'reports/assets/P9-03/r1';E.mkdir(exist_ok=True)
def load(p):return json.loads(p.read_text())
def save(p,j):p.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n')
p=argparse.ArgumentParser();p.add_argument('sid',choices=['B2','B3']);p.add_argument('--attempt',type=int,default=1);args=p.parse_args()
assert args.attempt in [1,2]
r=load(S/f'{args.sid}.json');cfg=load(ROOT/r['task_root']/'state.json')['config'];specfile=E/f'{args.sid}-attempt{args.attempt}.json'
usagefile=E/'usage.json'
u=load(usagefile) if usagefile.exists() else dict(started_epoch=time.time(),submissions=0,generated_images=0,review_calls=0,vlm_calls=0)
assert u['submissions']<4 and time.time()-u['started_epoch']<1700
if specfile.exists():raise RuntimeError('Existing attempt: inspect prompt_id/history before resuming; do not resubmit')
if args.attempt==2 and not r.get('retry_diagnosis'):raise RuntimeError('Retry requires saved diagnosis and revised instruction')
identity=r['identity'];outfit=r['shot_requirement']['outfit'];prompt=('Add a second separate physical instance of the exact same woman next to the original woman in this image. Show TWO women together, side by side in the same scene, both clearly visible. The second is the SAME character, with exactly the same face, hair, eye color, accessories and outfit, but a different pose: one holds her existing pose, the other raises one hand. This is not a mirror, reflection, poster, shadow or split panel. Recompose and zoom out as needed to fit both women. Preserve the original character and the scene style. Identity details: '+identity['identity_block']+'. Clothing: '+', '.join(outfit['tags'])+'.')
if args.attempt==2:prompt=r['retry_instruction']
client=ComfyClient(load(ROOT/'calibration/spec.json')['comfyui_url'],180)
try:
 stats=client.http.get('/system_stats');stats.raise_for_status();save(E/'system_stats.json',stats.json())
 upload=client.upload_image(ROOT/r['source_image'],subfolder='P9_03_r1');name='/'.join(filter(None,[upload.get('subfolder'),upload['name']]))
 with Image.open(ROOT/r['source_image']) as im:w,h=im.size
 wf=build_qwen_workflow(cfg,name,prompt,w,h,202610010300+int(args.sid[1:]),f'storycg_P9_03_r1_{args.sid}_a{args.attempt:03d}')
 record=dict(id=args.sid,attempt=args.attempt,source_image=r['source_image'],source_sha256=r['source_sha256'],instruction=prompt,workflow=wf,upload=upload,status='submitting',started_epoch=time.time())
 save(specfile,record);u['submissions']+=1;save(usagefile,u)
 pid=client.submit_prompt(wf,'P9-03-r1');record.update(prompt_id=pid,status='running');save(specfile,record);print('submitted',args.sid,pid,flush=True)
 history=client.wait_for_history(pid,2,max(1, min(1200,1700-(time.time()-u['started_epoch']))));save(E/f'{args.sid}-attempt{args.attempt}-history.json',history)
 if not history or history.get('status',{}).get('status_str')=='error':raise RuntimeError('Missing or failed history; inspect persisted attempt, do not blindly resubmit')
 images=history['outputs']['9']['images'];assert len(images)==1
 dest=E/f'{args.sid}-attempt{args.attempt}.png';dest.write_bytes(client.download_image(images[0]));record.update(status='completed',image_path=str(dest.relative_to(ROOT)),image_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),comfy_output=images[0],elapsed_seconds=round(time.time()-record['started_epoch'],2));save(specfile,record)
 u['generated_images']+=1;u['elapsed_seconds']=round(time.time()-u['started_epoch'],2);save(usagefile,u);print('completed',args.sid,record['elapsed_seconds'],flush=True)
finally:client.close()
