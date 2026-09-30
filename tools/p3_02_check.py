"""Read-only verification of completed formal CLI comparisons and real history."""
import argparse
import hashlib
import json
from pathlib import Path
from storycg.comfy import ComfyClient
from storycg.config import load_config


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check(groups, config):
    generated=posts=vision=llm=0; jobs=[]
    client=ComfyClient(config['comfyui_url'],config['http_timeout_seconds'])
    try:
        for group in groups:
            boards=[sha(root/'storyboard.json') for root in group]
            assert len(set(boards))==1,'storyboard mismatch'
            manifests=[read(root/'manifest.json') for root in group]
            seeds=[[i['seed'] for i in m['images']] for m in manifests]
            assert all(s==seeds[0] for s in seeds),'seed mismatch'
            portraits=[read(root/'portrait.json') for root in group]
            assert len({p['image']['sha256'] for p in portraits})==1,'portrait mismatch'
            for root,m,portrait in zip(group,manifests,portraits):
                s=read(root/'state.json');assert len(m['images'])==4
                assert m['identity_control']==s['config']['identity_control']
                assert m['layout_mode']==s['config']['layout_mode']
                assert read(root/'review.json')['reference']['image']['sha256']==portrait['image']['sha256']
                assert len(m['review_records'])==1,'one complete audit per job required'
                states=[(root,s)]
                if (root/'portrait/state.json').exists():states.append((root/'portrait',read(root/'portrait/state.json')))
                for folder,state in states:
                    for a in state['attempts']:
                        assert a['status']=='completed'
                        record=client.get_history(a['prompt_id'])[a['prompt_id']]
                        assert record['status']['status_str']=='success' and record['status']['completed']
                        assert record['prompt'][2]==a['workflow'],'actual workflow mismatch'
                        assert a['image']['sha256']==sha(folder/a['image']['path'])
                        assert a['image']['comfy_output']['filename'].startswith(a['filename_prefix'])
                        generated+=1;posts+=a['post_count']
                for image in m['images']:
                    a=next(a for a in s['attempts'] if a['shot_id']==image['shot_id'])
                    assert image['reference']==a['reference']
                    assert image['seed']==a['seed'] and image['sha256']==sha(root/image['path'])
                    ref=image['reference'];wf=a['workflow']
                    if ref is None:
                        assert m['identity_control']['mechanism']=='none' and m['layout_mode']=='none'
                        assert not any(n['class_type']=='IPAdapterAdvanced' for n in wf.values())
                        continue
                    assert ref['sha256']==sha(root/ref['path'])
                    if ref['mechanism']=='img2img':
                        layout=next(i for i in m['layout_images'] if i['prompt_id']==ref['prompt_id'])
                        assert ref['path']==layout['path'] and ref['sha256']==layout['sha256']
                        assert ref['upstream_reference']==layout['reference']
                        assert ref['upstream_reference']['sha256']==portrait['image']['sha256']
                        assert wf['10']['inputs']['image']==ref['uploaded_image']
                        assert wf['7']['inputs']['latent_image']==['12',0] and wf['7']['inputs']['denoise']==0.55
                        identity_ref=ref['identity_reference']
                    else:identity_ref=ref
                    if identity_ref:
                        assert identity_ref['sha256']==portrait['face']['sha256']
                        assert identity_ref['strength']==m['identity_control']['ipadapter_strength']==0.4
                        node=wf[wf['7']['inputs']['model'][0]]
                        assert node['class_type']=='IPAdapterAdvanced' and node['inputs']['weight']==0.4
                        assert wf[node['inputs']['image'][0]]['inputs']['image']==identity_ref['uploaded_image']
                log=root/'review_log.jsonl'
                if log.exists():
                    records=[json.loads(l) for l in log.read_text().splitlines()]
                    vision+=sum(r['event']=='request' for r in records)
                    for r in records:
                        if r['event']=='request' and r['purpose']=='narrative_blind':
                            assert all(c['type']=='image_url' for c in r['request']['messages'][0]['content'])
                plan=root/'plan_log.json'
                if plan.exists():llm+=len(read(plan)['calls'])
                jobs.append({'job':str(root),'images':len(m['images']),'layout_images':len(m['layout_images']),
                             'review':read(root/'review.json')['layer_conclusions'],'seeds':seeds[0]})
        assert generated<=32 and posts<=32 and vision<=90 and llm<=15
        return {'verified':True,'jobs':jobs,'usage':{'generated_images':generated,'submission_attempts':posts,
            'model_visual_posts':vision,'llm_calls':llm},'note':'Does not include executor view_image calls; add them separately.'}
    finally:client.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',action='append',nargs='+',required=True,type=Path)
    p.add_argument('--config',default='config/default.json',type=Path)
    a=p.parse_args();print(json.dumps(check(a.group,load_config(a.config)),ensure_ascii=False,indent=2))
