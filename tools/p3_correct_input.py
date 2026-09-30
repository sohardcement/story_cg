"""Correct a diagnosed cross-shot latent input mistake; never hide prior outputs."""
import copy,json,sys,time
from pathlib import Path
from storycg.assembly import assemble_prompt
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.reference import require_portrait,digest
from storycg.runner import run_gen_one
from storycg.state import save_json_atomic
from p3_pilot import upload,manifest,grid


def run(root,number):
    root=Path(root).resolve();spec=json.loads((root/'pilot.json').read_text());portrait=require_portrait(root)
    cfg=load_config(spec['config_file']);board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text())
    entry=next(e for e in board['shots'] if e['number']==number)
    m=manifest(root);shot=f'shot{number:02d}'
    layout=next(r for r in m['images'] if r['cell']=='m2_layout_repair' and r['shot']==shot)
    wrong=next(r for r in m['images'] if r['cell']=='m2_repair' and r['shot']==shot)
    if m['usage']['submission_attempts']>=spec['max_submissions']:raise ValueError('pilot budget exhausted')
    if time.time()>spec['deadline_epoch']:raise ValueError('pilot time exhausted')
    client=ComfyClient(cfg['comfyui_url'],180)
    try:
        relation=copy.deepcopy(wrong['reference'])
        relation.update(path=layout['image']['path'],sha256=layout['image']['sha256'],uploaded_image=upload(root,root/layout['image']['path'],client),upstream_reference=layout['reference'])
        diagnosis={'diagnosis':'Saved per-task configuration reused the first shot latent source for later shots.',
            'hypothesis':'Explicit per-shot reference overrides will bind each WAI shot to its own Qwen layout.',
            'changes':'Fix runner reference override and rerender only affected shot; same seed/weights/denoise/prompt.',
            'previous_image':wrong['image'],'previous_actual_reference':wrong['reference'],'corrected_reference':relation}
        child=root/'m2_corrected';child.mkdir(exist_ok=True)
        c=copy.deepcopy(cfg);c['generation_seed']=spec['seed'];c['auto_retry_failed']=False;c['generation_reference']=relation;c['prefix_namespace']=root.name+'_m2_corrected'
        c['budget'].update(max_images_per_job=1,max_job_minutes=max(1,(spec['deadline_epoch']-time.time())/60))
        parts=assemble_prompt(identity,entry,props=board['props'],config=c)
        save_json_atomic(child/f'input_{shot}.json',{'identity':identity,'entry':entry,'props':board['props'],'parts':parts,'repair_diagnosis':diagnosis})
        state=run_gen_one(child,shot,parts['positive_tags'],parts['framing'],c,client,negative_prompt=', '.join(filter(None,[c['negative_prompt'],parts['negative_tags']])),generation_reference=relation)
        a=state['attempts'][-1]
        if a['status']!='completed':raise ValueError('correction failed')
        diagnosis['result']={'image':a['image'],'prompt_id':a['prompt_id'],'actual_reference':a['reference']}
        save_json_atomic(root/'input_reference_correction.json',diagnosis)
        spec.setdefault('m2_final_shots',{})[str(number)]='m2_corrected'
        save_json_atomic(root/'pilot.json',spec);grid(root,spec);manifest(root)
        print('corrected',shot,a['prompt_id'],flush=True)
    finally:client.close()

if __name__=='__main__':run(sys.argv[1],int(sys.argv[2]))
