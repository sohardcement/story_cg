"""Serial, resumable style experiment driven by a persisted task specification."""
import copy
import hashlib
import json
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from storycg.assembly import assemble_prompt
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.runner import run_gen_one
from storycg.state import save_json_atomic


def run(root):
    root=Path(root)
    spec=json.loads((root/'experiment.json').read_text())
    config=load_config(spec['config_file'])
    client=ComfyClient(config['comfyui_url'],180)
    start=time.monotonic()
    for cell in spec['cells']:
        job=root/cell['id']
        cfg=copy.deepcopy(config)
        for lora,weight in zip(cfg['style_loras'],cell['weights']):
            lora.update(strength_model=weight,strength_clip=weight)
        # Record the effective experimental style and its canonical hash, separately from source file.
        effective=copy.deepcopy(cfg['style_snapshot']['content'])
        effective['style_loras']=copy.deepcopy(cfg['style_loras'])
        cfg['experimental_style']={'content':effective,'sha256':hashlib.sha256(json.dumps(effective,sort_keys=True).encode()).hexdigest()}
        cfg['generation_seed']=spec['seed']
        cfg['budget'].update(max_images_per_job=2,max_job_minutes=60)
        total=sum(a.get('post_count',0) for p in root.glob('*/state.json') for a in json.loads(p.read_text())['attempts'])
        if total>spec['max_submissions']: raise ValueError('global submission budget exceeded')
        for entry in spec['shots']:
            parts=assemble_prompt(spec['identity'],entry,props=spec['props'],config=cfg)
            job.mkdir(exist_ok=True)
            save_json_atomic(job/f'input_{entry["id"]}.json',{'identity':spec['identity'],'entry':entry,'props':spec['props'],'prompt_parts':parts,'weights':cell['weights']}) if not (job/f'input_{entry["id"]}.json').exists() else None
            shot_id=entry['id']
            existing=json.loads((job/'state.json').read_text()) if (job/'state.json').exists() else {'attempts':[]}
            if not any(a['shot_id']==shot_id for a in existing['attempts']) and total>=spec['max_submissions']:
                raise ValueError('global submission budget exhausted')
            state=run_gen_one(job,shot_id,parts['positive_tags'],parts['framing'],cfg,client,negative_prompt=', '.join(filter(None,[cfg['negative_prompt'],parts['negative_tags']])))
            latest=state['attempts'][-1]
            if latest['status']!='completed': raise ValueError(f'non-completed attempt: {latest["status"]}')
            print(cell['id'],shot_id,latest['prompt_id'],flush=True)
            total=sum(a.get('post_count',0) for p in root.glob('*/state.json') for a in json.loads(p.read_text())['attempts'])
    client.close()
    assemble_grid(root,spec)
    print('elapsed_seconds',round(time.monotonic()-start,2),flush=True)


def assemble_grid(root,spec):
    font=ImageFont.truetype('/usr/share/fonts/liberation/LiberationSans-Regular.ttf',26)
    records=[]
    for shot in spec['shots']:
        w,h=520,650
        sheet=Image.new('RGB',(3*w,4*h),'#eeeeee')
        draw=ImageDraw.Draw(sheet)
        for index,cell in enumerate(spec['cells']):
            job=root/cell['id']; state=json.loads((job/'state.json').read_text())
            a=next(a for a in state['attempts'] if a['shot_id']==shot['id'])
            x=(index%3)*w if index<9 else 0; y=(index//3)*h if index<9 else 3*h
            label=f"{shot['id']}  Iwao178={cell['weights'][0]:.1f}  akaimelon={cell['weights'][1]:.1f}"
            draw.text((x+10,y+12),label,fill='black',font=font)
            with Image.open(job/a['image']['path']) as im: thumb=ImageOps.contain(im.convert('RGB'),(w-16,h-60))
            sheet.paste(thumb,(x+(w-thumb.width)//2,y+55))
            records.append({'cell':cell['id'],'weights':cell['weights'],'shot':shot['id'],'path':str(job/a['image']['path']),'prompt_parts':assemble_prompt(spec['identity'],shot,props=spec['props'],config=state['config']),'seed':a['seed'],'prompt_id':a['prompt_id'],'sha256':a['image']['sha256'],'workflow':a['workflow'],'style':state['config']['style_snapshot'],'experimental_style':state['config']['experimental_style']})
        draw.text((w+30,3*h+80),'Baseline: both LoRAs disabled',font=font,fill='black')
        draw.text((w+30,3*h+125),f"Seed {spec['seed']} / rows Iwao / columns akai",font=font,fill='black')
        sheet.save(f"review/P2-grid-{shot['id']}.jpg",quality=94,subsampling=0)
    states=[json.loads(p.read_text()) for p in root.glob('*/state.json')]
    save_json_atomic(root/'manifest.json',{'experiment':spec,'images':records,'usage':{'generated_images':sum(s['stats']['generated_images'] for s in states),'submission_attempts':sum(s['stats']['submission_attempts'] for s in states),'review_calls':0}})

if __name__=='__main__': run(sys.argv[1])
