"""Read-only local/remote evidence check; never submits generation."""
import copy
import hashlib
import json
import sys
from pathlib import Path
from PIL import Image
from storycg.assembly import assemble_prompt
from storycg.comfy import ComfyClient

root=Path(sys.argv[1]); manifest=json.loads((root/'manifest.json').read_text()); spec=manifest['experiment']
assert len(manifest['images'])==20
assert manifest['usage']['generated_images']==manifest['usage']['submission_attempts']==20
assert manifest['usage']['review_calls']==0
client=None
normalized={}; prompts={}; rows=[]
for cell in spec['cells']:
    job=root/cell['id']; state=json.loads((job/'state.json').read_text()); cfg=state['config']
    if client is None: client=ComfyClient(cfg['comfyui_url'],180)
    source=cfg['style_snapshot']
    assert hashlib.sha256(json.dumps(cfg['experimental_style']['content'],sort_keys=True).encode()).hexdigest()==cfg['experimental_style']['sha256']
    assert source==manifest['images'][0]['style']
    assert cfg['checkpoint']=='waiIllustriousSDXL_v170.safetensors'
    assert [x['name'] for x in cfg['style_loras']]==['Iwao178.safetensors','akaimelon.safetensors']
    assert len(state['attempts'])==2
    for entry in spec['shots']:
        a=next(a for a in state['attempts'] if a['shot_id']==entry['id']); w=a['workflow']
        assert a['status']=='completed' and a['post_count']==1
        assert a['seed']==w['7']['inputs']['seed']==spec['seed']
        parts=assemble_prompt(spec['identity'],entry,props=spec['props'],config=cfg)
        assert w['4']['inputs']['text']==cfg['positive_prefix']+', '+parts['positive_tags']
        assert w['5']['inputs']['text']==', '.join(filter(None,[cfg['negative_prompt'],parts['negative_tags']]))
        for node,weight in zip(('2','3'),cell['weights']):
            assert w[node]['inputs']['strength_model']==w[node]['inputs']['strength_clip']==weight
        normal=copy.deepcopy(w)
        for node in ('2','3'):
            normal[node]['inputs']['strength_model']=normal[node]['inputs']['strength_clip']=0
        normal['9']['inputs']['filename_prefix']='normalized'
        if entry['id'] in normalized: assert normal==normalized[entry['id']]
        else: normalized[entry['id']]=normal
        path=job/a['image']['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==a['image']['sha256']
        with Image.open(path) as im:
            assert im.format=='PNG' and list(im.size)==cfg['resolutions'][entry['shot']['framing']]
            im.verify()
        history=client.get_history(a['prompt_id'])[a['prompt_id']]
        assert history['status']['status_str']=='success' and history['status']['completed']
        assert history['prompt'][2]==w
        rows.append({'cell':cell['id'],'shot':entry['id'],'prompt_id':a['prompt_id'],'status':'success','seed':a['seed'],'path':str(path)})
for shot in ('A','B'):
    with Image.open(f'review/P2-grid-{shot}.jpg') as im: assert im.size==(1560,2600)
client.close()
print(json.dumps({'verified':len(rows),'workflow_comparison':'only two LoRA strengths and filename_prefix vary within each shot','records':rows},indent=2))
