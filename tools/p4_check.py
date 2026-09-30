"""Read-only artifact verification and report rendering; never invokes a model."""
import json,hashlib
from pathlib import Path
from PIL import Image
root=Path('calibration');spec=json.loads((root/'spec.json').read_text());expected={f'E{i}{s}' for i in range(1,8) for s in 'ab'}|{f'C{i}' for i in range(1,7)}|{'Q1','Q2'}
assert {s['id'] for s in spec['samples']}==expected
for sid in sorted(expected):
 p=root/'samples'/f'{sid}.png';j=json.loads(p.with_suffix('.json').read_text())
 with Image.open(p) as im:im.verify()
 assert j['id']==sid
 assert hashlib.sha256(p.read_bytes()).hexdigest()==j['image_sha256']
 assert j['source_image'] and j['shot_requirement'] and j['identity']
 assert 'edit_effective' in j and 'executor_observation' in j
 if j['method']!='copy':assert j['workflow'] and j['prompt_id'] and j['status']=='completed'
 else:assert p.read_bytes()==Path(j['source_image']).read_bytes()
 assert j['supervisor_confirmed'] is False
 assert j['method']=='copy' or j['edit_effective'] in (True,False,None)
assert len(list((root/'samples').glob('*.png')))==22
assert len(list((root/'samples').glob('*.json')))==22
usage=json.loads((root/'usage.json').read_text());assert usage['submissions']<=28 and usage['review_calls']==usage['vlm_calls']==0
assert usage['generated_images']==sum(x['method']!='copy' for x in spec['samples'])
with Image.open('review/P4-samples.jpg') as im:im.verify()
manifest={'samples':[json.loads((root/'samples'/f'{sid}.json').read_text()) for sid in sorted(expected)],'usage':usage,'overview':'review/P4-samples.jpg','supervisor_confirmation_required':True}
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'sample_count':len(expected),'pngs':22,'jsons':22,'usage':usage,'overview':'review/P4-samples.jpg','manifest':'calibration/manifest.json'},ensure_ascii=False,indent=2))
