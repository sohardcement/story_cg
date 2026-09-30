"""Serial reference pilot. All character/scene/prop specifics come from job JSON."""
import copy
import json
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from storycg.assembly import assemble_prompt
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.reference import require_portrait, digest
from storycg.runner import run_gen_one
from storycg.state import save_json_atomic


def upload(root, path, client):
    cache=root/'uploads.json'
    records=json.loads(cache.read_text()) if cache.exists() else {}
    key=digest(path)
    if key not in records:
        result=client.upload_image(path,subfolder=root.name)
        records[key]='/'.join(filter(None,[result.get('subfolder'),result['name']]))
        save_json_atomic(cache,records)
    return records[key]


def manifest(root):
    records=[];submissions=0
    for path in root.glob('*/state.json'):
        state=json.loads(path.read_text())
        submissions+=state['stats']['submission_attempts']
        for a in state['attempts']:
            records.append({'cell':path.parent.name,'shot':a['shot_id'],'status':a['status'],
                'seed':a['seed'],'prompt_id':a['prompt_id'],'workflow':a['workflow'],'reference':a.get('reference'),
                'image':{**a.get('image',{}),'path':str(path.parent.relative_to(root)/a['image']['path'])} if a.get('image') else None,
                'elapsed_seconds':( __import__('datetime').datetime.fromisoformat(a.get('completed_at',a['created_at']))-__import__('datetime').datetime.fromisoformat(a['created_at'])).total_seconds(),
                'style':state['config'].get('style_snapshot'),'error':a.get('error')})
    logs=[json.loads(line) for line in (root/'review_log.jsonl').read_text().splitlines()] if (root/'review_log.jsonl').exists() else []
    spec=json.loads((root/'pilot.json').read_text()) if (root/'pilot.json').exists() else {}
    portrait=json.loads((root/'portrait.json').read_text())
    grid_cells=[]
    for row,cell in enumerate(['baseline','m1_low','m1_high',spec.get('m2_final_cell','m2')]):
        grid_cells.append({'row':row,'column':0,'cell':cell,'shot_number':0,'image':portrait['image'],'generation_reference':None,'review_reference':portrait['image']})
        for col,n in enumerate(spec.get('shot_numbers',[]),1):
            effective_cell=spec.get('m2_final_shots',{}).get(str(n),cell) if row==3 else cell
            match=next((r for r in records if r['cell']==effective_cell and r['shot']==f'shot{n:02d}' and r['status']=='completed'),None)
            grid_cells.append({'row':row,'column':col,'cell':effective_cell,'shot_number':n,'image':match['image'] if match else None,'generation_reference':match['reference'] if match else None,'review_reference':portrait['image']})
    inventory={}
    fields={'CheckpointLoaderSimple':'ckpt_name','LoraLoader':'lora_name','LoraLoaderModelOnly':'lora_name',
            'UnetLoaderGGUF':'unet_name','CLIPLoader':'clip_name','CLIPVisionLoader':'clip_name',
            'VAELoader':'vae_name','IPAdapterModelLoader':'ipadapter_file'}
    for record in records:
        hashes=(record.get('style') or {}).get('content',{}).get('model_hashes',{})
        for node in record['workflow'].values():
            field=fields.get(node['class_type'])
            if field:
                name=node['inputs'][field]
                inventory[name]={'file':name,'loader':node['class_type'],'sha256':hashes.get(name,{}).get('sha256','unavailable'),
                                 'hash_reason':'Remote files are not mounted; no whole-file SHA256 obtained in this trial.'}
    result={'model_inventory':list(inventory.values()),'portrait':portrait,'images':records,'comparison_grid':{'path':'review/P3-01-pilot.jpg','cells':grid_cells},
            'source_files':{name:{'path':name,'sha256':digest(root/name)} for name in ['identity.json','storyboard.json','config.json','pilot.json'] if (root/name).exists()},
            'review_records': {'portrait':'portrait_attempts.json','consistency':'comparison_review.json','raw_log':'review_log.jsonl'},
            'usage':{'generated_images':sum(r['status']=='completed' for r in records),'submission_attempts':submissions,'review_calls':sum(r['event']=='request' for r in logs),
                     'review_call_seconds':round(sum(r.get('elapsed_seconds',0) for r in logs if r['event']=='response'),3),
                     'executor_image_inspections':spec.get('executor_image_inspections',0),
                     'visual_calls_including_inspections':sum(r['event']=='request' for r in logs)+spec.get('executor_image_inspections',0)}}
    save_json_atomic(root/'manifest.json',result)
    return result


def generate(root):
    root=Path(root).resolve();portrait=require_portrait(root)
    spec=json.loads((root/'pilot.json').read_text());cfg=load_config(spec['config_file'])
    identity=json.loads((root/'identity.json').read_text());board=json.loads((root/'storyboard.json').read_text())
    entries=[next(e for e in board['shots'] if e['number']==n) for n in spec['shot_numbers']]
    client=ComfyClient(cfg['comfyui_url'],180)
    face=upload(root,root/portrait['face']['path'],client)
    full=upload(root,root/portrait['image']['path'],client)
    def one(cell,entry,relation=None,prompt=None):
        require_portrait(root)
        m=manifest(root)
        if m['usage']['submission_attempts']>=spec['max_submissions']:
            raise ValueError('pilot submission budget exhausted')
        if time.time()>spec['deadline_epoch']: raise ValueError('pilot time budget exhausted')
        c=copy.deepcopy(cfg);c['generation_seed']=spec['seed'];c['auto_retry_failed']=False
        c['prefix_namespace']=root.name+'_'+cell
        c['budget'].update(max_images_per_job=2,max_job_minutes=max(1,(spec['deadline_epoch']-time.time())/60))
        if relation: c['generation_reference']=relation
        parts=assemble_prompt(identity,entry,props=board['props'],config=c)
        tags=prompt or parts['positive_tags'];negative=', '.join(filter(None,[c['negative_prompt'],parts['negative_tags']]))
        if relation and relation['mechanism']=='qwen_edit': negative=None
        child=root/cell;child.mkdir(exist_ok=True)
        save_json_atomic(child/f"input_shot{entry['number']:02d}.json",{'identity':identity,'entry':entry,'props':board['props'],'parts':parts,'edit_prompt':prompt})
        before=time.monotonic()
        state=run_gen_one(child,f"shot{entry['number']:02d}",tags,parts['framing'],c,client,negative_prompt=negative,generation_reference=relation)
        a=next(a for a in reversed(state['attempts']) if a['shot_id']==f"shot{entry['number']:02d}")
        manifest(root)
        print(cell,entry['number'],a['status'],round(time.monotonic()-before,2),flush=True)
        if a['status']!='completed': raise ValueError(f"failed pilot cell {cell}: {a.get('error')}")
        return child/a['image']['path']
    common={'source_task':portrait['source_task'],'portrait_path':portrait['image']['path'],'portrait_sha256':portrait['image']['sha256']}
    try:
        for cell,strength in [('baseline',None),('m1_low',0.4),('m1_high',0.7)]:
            for entry in entries:
                relation=None if strength is None else {**common,'mechanism':'ipadapter','strength':strength,'uploaded_image':face,'path':portrait['face']['path'],'sha256':portrait['face']['sha256']}
                one(cell,entry,relation)
        for entry in entries:
            # Natural language uses only the persisted shot, scene, outfit and referenced props.
            selected={p['id']:p for p in board['props']}
            payload={'scene':entry['scene'],'outfit':entry['outfit'],'shot':entry['shot'],
                     'props':[selected[r['prop_id']] for r in entry['shot']['key_props']]}
            prompt='Edit the input character portrait into this single scene. Preserve the same identity, face, eye color, hairstyle, ears, accessories and complete outfit. Change pose, camera and background to the supplied state. Draw only one protagonist. No captions or panel borders. Scene state: '+json.dumps(payload,ensure_ascii=False)
            relation={**common,'mechanism':'qwen_edit','strength':1.0,'uploaded_image':full,'path':portrait['image']['path'],'sha256':portrait['image']['sha256'],'lightning_strength':1.0}
            layout=one('m2_layout',entry,relation,prompt)
            image=upload(root,layout,client)
            one('m2',entry,{**common,'mechanism':'img2img','strength':0.55,'uploaded_image':image,'path':str(layout.relative_to(root)),'sha256':digest(layout),'upstream_reference':relation})
        grid(root,spec)
    finally:
        client.close();manifest(root)


def grid(root,spec):
    m=manifest(root);w,h=500,620
    sheet=Image.new('RGB',(w*3,h*4),'#eeeeee');draw=ImageDraw.Draw(sheet)
    font=ImageFont.truetype('/usr/share/fonts/liberation/LiberationSans-Regular.ttf',23)
    portrait=require_portrait(root)
    for row,(cell,label) in enumerate([('baseline','Baseline / no generation ref'),('m1_low','M1 IPAdapter 0.4'),('m1_high','M1 IPAdapter 0.7'),(spec.get('m2_final_cell','m2'),'M2 Qwen + WAI 0.55 / repair' if spec.get('m2_final_cell') else 'M2 Qwen + WAI denoise 0.55')]):
        paths=[root/portrait['image']['path']]
        paths.extend(root/next(r['image']['path'] for r in m['images'] if r['cell']==(spec.get('m2_final_shots',{}).get(str(n),cell) if row==3 else cell) and r['shot']==f'shot{n:02d}') for n in spec['shot_numbers'])
        for col,path in enumerate(paths):
            x,y=col*w,row*h
            draw.text((x+8,y+8),'Approved portrait' if col==0 else label,font=font,fill='black')
            if col: draw.text((x+8,y+38),f"Shot {spec['shot_numbers'][col-1]}{' (input fix)' if row==3 and str(spec['shot_numbers'][col-1]) in spec.get('m2_final_shots',{}) else ''} / {spec['seed']}",font=font,fill='black')
            with Image.open(path) as im:thumb=ImageOps.contain(im.convert('RGB'),(w-12,h-76))
            sheet.paste(thumb,(x+(w-thumb.width)//2,y+72))
    sheet.save('review/P3-01-pilot.jpg',quality=94,subsampling=0)

if __name__=='__main__':generate(sys.argv[1])
