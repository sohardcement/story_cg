"""Two serial, one-attempt detection-mask experiments; new job copies only."""
import json
from pathlib import Path
import time
from datetime import datetime,timezone
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from storycg.repairs import fork_repair_job,repair,record_result
from storycg.detect import ROOT,detect_image,json_result,draw_overlay
from storycg.detection_review import repair_mask,audit_detection
from storycg.cycle import audit_candidate
from storycg.review import VisionClient
from storycg.comfy import ComfyClient
from storycg.reference import digest
from storycg.state import save_json_atomic,load_state

SPEC=[
 {'id':'duplicate','source':'jobs/cg-76d2eec57dfc','shot':1,'attempt':1,'file':'storycg_cg-76d2eec57dfc_shot01_a001.png','purpose':'remove_person','primitive':'inpaint',
  'diagnosis':'Major faces show two foreground subjects; person segmentation merged both subjects and background pixels.',
  'hypothesis':'Face-seeded spatial partition of merged segmentation may remove the smaller-face extra person; ownership is uncertain and primary limbs may be affected.',
  'changes':'One detector-derived feathered mask, background-only positive conditioning and person-negative conditioning; no IPAdapter in the removal region.'},
 {'id':'face','source':'runs/p3_02_a_none','shot':3,'attempt':1,'file':'storycg_p3_02_a_none_shot03_a001.png','purpose':'face','primitive':'face_detail',
  'diagnosis':'Existing review.json and P3-02 human inspection record blue eyes against locked green eyes on a visible face.',
  'hypothesis':'Local detected face mask plus a freshly detected setting-face IPAdapter reference may correct the iris color while retaining context.',
  'changes':'One detected face mask through latent noise masking and pixel compositing; aspect preserved with context margin and the existing face-detail denoise.'}
]


def main():
    started=time.monotonic();folder=ROOT/'reports/assets/P9-02-live';folder.mkdir(parents=True,exist_ok=True)
    ledger_path=folder/'usage.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {'submission_attempts':0,'vision_calls':0,'started_at':datetime.now(timezone.utc).isoformat()}
    class LimitedComfy(ComfyClient):
        def submit_prompt(self,workflow,client_id):
            if ledger['submission_attempts']>=8:raise ValueError('P9-02 image budget exhausted')
            if time.monotonic()-started>=90*60:raise ValueError('P9-02 wall-clock budget exhausted')
            ledger['submission_attempts']+=1;save_json_atomic(ledger_path,ledger)
            return super().submit_prompt(workflow,client_id)
    client=LimitedComfy('http://127.0.0.1:8188',180)
    outputs=[]
    try:
        for spec in SPEC:
            source=ROOT/spec['source'];root=ROOT/'runs'/('p9_02_'+spec['id']);original=source/spec['file']
            input_sha=digest(original)
            fork_repair_job(source,root)
            state=load_state(root);state['config']['budget']['max_job_minutes']=60
            # The frozen visual prompts/config remain unchanged; only new-job runtime budget changes.
            save_json_atomic(root/'state.json',state)
            image=root/spec['file'];result=detect_image(image)
            region=repair_mask(image,result,spec['purpose'],root/'p9_detection')
            draw_overlay(image,result).save(root/'p9_detection'/'overlay.jpg')
            save_json_atomic(root/'p9_detection'/'detections.json',json_result(result,root/'p9_detection'/'persons'))
            board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text());portrait=json.loads((root/'portrait.json').read_text())
            before=audit_detection(image,identity,board['shots'][spec['shot']-1],reference_path=root/portrait['image']['path'],result=result)
            save_json_atomic(root/'p9_before.json',before)
            diagnosis={k:spec[k] for k in ('diagnosis','hypothesis','changes')};diagnosis['result']={'status':'pending'}
            kwargs={'bbox':region['bbox'],'mask_path':region['mask_path'],'mask_sha256':region['mask_sha256'],
                    'region_source':region['provenance'],'repair_id':'p9_02','source_attempt':spec['attempt']}
            if spec['purpose']=='remove_person':kwargs['operation']={'type':'remove_person'}
            print('Submitting single experiment',spec['id'],region['provenance'],flush=True)
            repair_started=time.monotonic()
            rec=repair(root,spec['shot'],spec['primitive'],diagnosis,client,**kwargs)
            seconds=time.monotonic()-repair_started
            if digest(original)!=input_sha:raise ValueError('read-only original changed')
            after=root/rec['output']['path']
            with Image.open(image) as im,Image.open(after) as edited,Image.open(root/rec['region']['mask']) as mask:
                a=np.asarray(im.convert('RGB'));b=np.asarray(edited.convert('RGB'));m=np.asarray(mask)
                outside=bool(np.array_equal(a[m==0],b[m==0]));changed=int(np.any(a!=b,axis=2).sum())
            if not outside:raise ValueError('mask-exterior pixels changed')
            remaining=20-ledger['vision_calls']
            if remaining<2:raise ValueError('P9-02 visual budget exhausted')
            vlm=VisionClient(root,'http://127.0.0.1:11234/v1',max_calls=min(4,remaining),max_seconds=600)
            try:
                audited=audit_candidate(root,spec['shot'],rec['output'],board['shots'][spec['shot']-1],board,identity,state['config'],vlm,'p9_after')
            finally:
                ledger['vision_calls']+=vlm.calls;save_json_atomic(ledger_path,ledger);vlm.close()
            record_result(root,f'shot{spec["shot"]:02d}_p9_02',{'file':'cycle_artifacts/p9_after.json','sha256':digest(root/'cycle_artifacts/p9_after.json')},
                          'One-attempt experiment. Frozen visual result and uncalibrated detector observations retained; human inspection pending.')
            row={**spec,'job':str(root.relative_to(ROOT)),'source_sha256':input_sha,'output':rec['output'],
                 'repair_seconds':seconds,'boundary_check':rec.get('boundary_check'),'mask_exterior_unchanged':outside,
                 'changed_pixels':changed,'region':rec['region'],'audit_file':str((root/'cycle_artifacts/p9_after.json').relative_to(ROOT)),
                 'detected_before':before,'detected_after':audited['detection_items'],'visual_conclusion':audited['conclusion']}
            outputs.append(row);save_json_atomic(folder/'results.json',{'results':outputs,'usage':ledger,'elapsed_seconds':time.monotonic()-started})
            print('Completed',spec['id'],'seconds',seconds,'output',after,'visual',audited['conclusion'],flush=True)
        sheet=Image.new('RGB',(1440,1120),'#20242b');draw=ImageDraw.Draw(sheet);font=ImageFont.load_default(size=24)
        for n,row in enumerate(outputs):
            paths=[ROOT/row['source']/row['file'],ROOT/row['job']/row['output']['path']]
            for c,path in enumerate(paths):
                with Image.open(path) as im:tile=ImageOps.contain(im.convert('RGB'),(710,505))
                x=c*720+(720-tile.width)//2;y=n*560+45+(505-tile.height)//2
                sheet.paste(tile,(x,y));draw.text((c*720+12,n*560+10),row['id']+' / '+('BEFORE' if c==0 else 'AFTER - one new attempt'),fill='white',font=font)
        sheet.save(ROOT/'review/P9-02-inpaint.jpg',quality=95)
    finally:client.close()

if __name__=='__main__':main()
