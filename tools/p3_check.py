"""Read-only verification of real generation evidence and reference graph."""
import hashlib,json,sys
from pathlib import Path
import httpx
from PIL import Image


def check(root):
    root=Path(root);m=json.loads((root/'manifest.json').read_text());spec=json.loads((root/'pilot.json').read_text())
    output=[]
    with httpx.Client(base_url='http://127.0.0.1:8188',timeout=180,trust_env=False) as client:
        for r in m['images']:
            assert r['status']=='completed',r
            path=root/r['image']['path'];assert hashlib.sha256(path.read_bytes()).hexdigest()==r['image']['sha256']
            with Image.open(path) as image:
                image.verify()
            record=client.get('/history/'+r['prompt_id']);record.raise_for_status();h=record.json()[r['prompt_id']]
            assert h['status']['status_str']=='success' and h['status']['completed']
            assert h['prompt'][2]==r['workflow']
            assert r['seed']==spec['seed']
            ref=r['reference'];wf=r['workflow']
            if ref:
                assert hashlib.sha256((root/ref['path']).read_bytes()).hexdigest()==ref['sha256']
                assert wf['10']['inputs']['image']==ref['uploaded_image']
                if ref['mechanism']=='ipadapter':
                    assert wf['7']['inputs']['model']==['12',0] and wf['12']['inputs']['image']==['10',0]
                    assert wf['12']['inputs']['weight']==ref['strength']
                elif ref['mechanism']=='qwen_edit':
                    assert wf['4']['inputs']['image1']==['10',0] and wf['5']['inputs']['image1']==['10',0]
                elif ref['mechanism']=='img2img':
                    assert wf['7']['inputs']['latent_image']==['12',0]
                    assert wf['12']['inputs']['pixels']==['11',0] and wf['11']['inputs']['image']==['10',0]
                    assert wf['7']['inputs']['denoise']==ref['strength']
                    upstream=ref['upstream_reference'];assert hashlib.sha256((root/upstream['path']).read_bytes()).hexdigest()==upstream['sha256']
                else:raise AssertionError('unknown mechanism')
            for name,weight in zip(['2','3'],[0.4,0.3]):
                if wf[name]['class_type']=='LoraLoader':
                    assert wf[name]['inputs']['strength_model']==weight and wf[name]['inputs']['strength_clip']==weight
            output.append({'cell':r['cell'],'shot':r['shot'],'prompt_id':r['prompt_id'],'verified':True,'seconds':r['elapsed_seconds']})
    logs=[json.loads(line) for line in (root/'review_log.jsonl').read_text().splitlines()]
    req=[r for r in logs if r['event']=='request'];res=[r for r in logs if r['event']=='response']
    assert len(req)==len(res)==m['usage']['review_calls']
    assert len(req)<=40 and len(output)<=22
    assert m['usage'].get('visual_calls_including_inspections',len(req))<=40
    assert len({r['call_id'] for r in req})==len(req)
    results=json.loads((root/'comparison_review.json').read_text())
    for cell in m['comparison_grid']['cells']:
        match=next(r for r in results if r['cell']==cell['cell'] and r['shot_number']==cell['shot_number'])
        assert match['source_sha256']==cell['image']['sha256']
        ref=cell['generation_reference']
        if ref and ref['mechanism']=='img2img':
            layout=next(r for r in m['images'] if r['cell']=='m2_layout_repair' and r['shot']==f"shot{cell['shot_number']:02d}")
            assert ref['sha256']==layout['image']['sha256'], 'wrong cross-shot img2img input'
    assert len(m['comparison_grid']['cells'])==12
    print(json.dumps({'images':output,'usage':m['usage'],'grid_cells_verified':12},indent=2))

if __name__=='__main__':check(sys.argv[1])
