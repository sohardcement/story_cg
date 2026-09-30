"""One diagnosed, serial M2 repair per shot; preserves all initial evidence."""
import copy,json,sys,time
from pathlib import Path
from storycg.assembly import assemble_prompt
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.reference import require_portrait,edit_instruction,digest
from storycg.runner import run_gen_one
from storycg.state import save_json_atomic
from p3_pilot import manifest,upload,grid


def run(root):
    root=Path(root).resolve();spec=json.loads((root/'pilot.json').read_text());portrait=require_portrait(root)
    cfg=load_config(spec['config_file']);identity=json.loads((root/'identity.json').read_text());board=json.loads((root/'storyboard.json').read_text())
    client=ComfyClient(cfg['comfyui_url'],180)
    full=upload(root,root/portrait['image']['path'],client)
    common={'source_task':portrait['source_task'],'portrait_path':portrait['image']['path'],'portrait_sha256':portrait['image']['sha256']}
    records=[]
    try:
        for n in spec['shot_numbers']:
            entry=next(e for e in board['shots'] if e['number']==n)
            prompt=edit_instruction(entry,board['props'])
            diagnosis={'diagnosis':'Initial two edits stayed nearly identical frontal compositions and failed object state/holder relationships.',
                'hypothesis':'Front-load action/framing and explicit object bindings; avoid verbose JSON distracting from the edit.',
                'changes':'Use compact instruction derived from unchanged storyboard; same seed/models/sampling/denoise.',
                'source':f'm2_layout/input_shot{n:02d}.json','new_prompt':prompt}
            relation={**common,'mechanism':'qwen_edit','strength':1.0,'uploaded_image':full,'path':portrait['image']['path'],'sha256':portrait['image']['sha256'],'lightning_strength':1.0}
            layout=None
            for cell in ['m2_layout_repair','m2_repair']:
                require_portrait(root)
                if manifest(root)['usage']['submission_attempts']>=spec['max_submissions']: raise ValueError('pilot budget exhausted')
                if time.time()>spec['deadline_epoch']:raise ValueError('pilot time exhausted')
                c=copy.deepcopy(cfg);c['auto_retry_failed']=False;c['generation_seed']=spec['seed']
                c['prefix_namespace']=root.name+'_'+cell
                c['budget'].update(max_images_per_job=2,max_job_minutes=max(1,(spec['deadline_epoch']-time.time())/60))
                if cell=='m2_repair':
                    relation={**common,'mechanism':'img2img','strength':0.55,'uploaded_image':upload(root,layout,client),'path':str(layout.relative_to(root)),'sha256':digest(layout),'upstream_reference':relation}
                c['generation_reference']=relation
                parts=assemble_prompt(identity,entry,props=board['props'],config=c)
                child=root/cell;child.mkdir(exist_ok=True)
                save_json_atomic(child/f'input_shot{n:02d}.json',{'identity':identity,'entry':entry,'props':board['props'],'parts':parts,'edit_prompt':prompt if cell=='m2_layout_repair' else None,'repair_diagnosis':diagnosis})
                start=time.monotonic()
                state=run_gen_one(child,f'shot{n:02d}',prompt if cell=='m2_layout_repair' else parts['positive_tags'],parts['framing'],c,client,negative_prompt=None if cell=='m2_layout_repair' else ', '.join(filter(None,[c['negative_prompt'],parts['negative_tags']])),generation_reference=relation)
                a=next(a for a in reversed(state['attempts']) if a['shot_id']==f'shot{n:02d}')
                print(cell,n,a['status'],round(time.monotonic()-start,2),flush=True);manifest(root)
                if a['status']!='completed':raise ValueError('repair generation failed')
                layout=child/a['image']['path']
                records.append({'cell':cell,'shot_number':n,'diagnosis':diagnosis,'image':str(layout.relative_to(root))})
                save_json_atomic(root/'m2_repair_diagnoses.json',records)
        spec['m2_final_cell']='m2_repair';save_json_atomic(root/'pilot.json',spec);grid(root,spec)
    finally:
        client.close();manifest(root)

if __name__=='__main__':run(sys.argv[1])
