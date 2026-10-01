"""Read-only evidence checks; writes one new validation record in reports/assets."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import numpy as np
from PIL import Image
from storycg.detect import ROOT
from storycg.repairs import mask_boundary_check


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    folder=ROOT/'reports/assets';live=json.loads((folder/'P9-02-live/results.json').read_text())
    replay=json.loads((folder/'P9-02-replay/results.json').read_text())
    ledger=json.loads((folder/'P9-02-live/usage.json').read_text())
    assert len(live['results'])==2 and len(replay['results'])==14
    assert ledger['submission_attempts']==2 and ledger['vision_calls']==4
    checked=[]
    for row in live['results']:
        root=ROOT/row['job'];source=ROOT/row['source']/row['file'];output=root/row['output']['path']
        assert sha(source)==row['source_sha256']==sha(root/row['file'])
        assert sha(output)==row['output']['sha256']
        assert row['output']['sha256']!=row['source_sha256']
        state=json.loads((root/'state.json').read_text());new=[a for a in state['attempts'] if not a.get('inherited')]
        assert len(new)==1 and new[0]['status']=='completed' and new[0]['post_count']==1
        workflow=new[0]['workflow'];latent=workflow['7']['inputs']['latent_image'][0]
        assert workflow[latent]['class_type']=='SetLatentNoiseMask'
        loader=workflow[latent]['inputs']['mask'][0]
        assert workflow[loader]['class_type']=='LoadImageMask'
        assert sha(root/row['region']['noise_mask']['path'])==row['region']['noise_mask']['sha256']
        mask_path=root/row['region']['mask'];assert sha(mask_path)==row['region']['mask_sha256']
        with Image.open(source) as base,Image.open(output) as after,Image.open(mask_path) as mask:
            assert base.size==after.size==mask.size
            x=np.array(base.convert('RGB'));y=np.array(after.convert('RGB'));m=np.array(mask.convert('L'))
            assert np.array_equal(x[m==0],y[m==0])
            edge=mask_boundary_check(base,after,mask)
        if row['id']=='duplicate':
            assert 'IPAdapterAdvanced' not in [n['class_type'] for n in workflow.values()]
            assert 'person' in workflow['5']['inputs']['text']
        else:
            assert 'IPAdapterAdvanced' in [n['class_type'] for n in workflow.values()]
            ref=new[0]['reference']['identity_reference'];assert ref['detection']['method']=='local_face_detector'
        manifest=json.loads((root/'manifest.json').read_text())
        assert manifest['usage']['generated_images']==1 and manifest['usage']['submission_attempts']==1
        audit=json.loads((ROOT/row['audit_file']).read_text())
        assert audit['image']['sha256']==row['output']['sha256']
        assert all(audit['detection_items'][k]['calibration']=='uncalibrated' for k in ('eye_color_sampled','accessory_color_sampled','protagonist_count'))
        checked.append({'id':row['id'],'prompt_id':new[0]['prompt_id'],'source_sha256':row['source_sha256'],
                        'output_sha256':row['output']['sha256'],'mask_exterior_unchanged':True,'mask_boundary_check':edge})
    for row in replay['results']:
        assert sha(ROOT/row['image'])==row['image_sha256']
        if row['reference_image']:assert sha(ROOT/row['reference_image'])==row['reference_sha256']
        thumb=row['blind_thumbnail'];path=Path(thumb['output_path'])
        assert sha(path)==thumb['output_sha256'] and max(thumb['output_size'])<=768
        with Image.open(path) as image:assert list(image.size)==thumb['output_size']
        for key in ('eye_color_sampled','accessory_color_sampled','protagonist_count'):
            assert row[key]['verified'] is False and row[key]['calibration']=='uncalibrated'
    assert replay['results'][0]['protagonist_count']['status']=='duplicate'
    assert all(r['protagonist_count']['status']=='ok' for r in replay['results'][1:])
    path=ROOT/'src/storycg/review.py'
    old=ast.parse(subprocess.check_output(['git','show','HEAD:src/storycg/review.py'],cwd=ROOT,text=True));new=ast.parse(path.read_text())
    for name in ('audit_single','audit_consistency','load_reliability','annotate_reliability','enforce_visibility','identity_checklist','grading_schema'):
        a=next(n for n in old.body if isinstance(n,ast.FunctionDef) and n.name==name)
        b=next(n for n in new.body if isinstance(n,ast.FunctionDef) and n.name==name)
        assert ast.dump(a,include_attributes=False)==ast.dump(b,include_attributes=False),name
    constants=lambda tree:{n.targets[0].id:ast.dump(n.value,include_attributes=False) for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)}
    assert constants(old)==constants(new)
    assert (ROOT/'config/review_reliability.json').read_bytes()==subprocess.check_output(['git','show','HEAD:config/review_reliability.json'],cwd=ROOT)
    models=json.loads((ROOT/'models/detect/manifest.json').read_text())
    for item in models['models']:
        weight=ROOT/'models/detect'/item['filename']
        assert weight.stat().st_size==item['size_bytes'] and sha(weight)==item['sha256']
    with Image.open(ROOT/'review/P9-02-inpaint.jpg') as sheet:assert sheet.size==(1440,1120)
    subprocess.run(['git','diff','--check'],cwd=ROOT,check=True)
    record={'validated':True,'live':checked,'replay_images':14,'generated_images':2,'vision_calls':4,
            'frozen_review_unchanged':True,'model_manifest_sha256':sha(ROOT/'models/detect/manifest.json'),
            'contact_sheet_sha256':sha(ROOT/'review/P9-02-inpaint.jpg')}
    (folder/'P9-02-validation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(record,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
