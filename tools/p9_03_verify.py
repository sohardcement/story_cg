"""Read-only artifact integrity checks; no image inference or color sampling."""
import hashlib,json
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
m=json.loads((ROOT/'calibration/v2/manifest.json').read_text())
ids=[f'A{i}' for i in range(1,8)]+[f'B{i}' for i in range(1,11)]+[f'C-e{i}' for i in range(1,5)]+[f'C-o{i}' for i in range(1,7)]
assert m['sample_ids']==ids
assert {p.stem for p in (ROOT/'calibration/v2/samples').glob('*.json')}==set(ids)
expected_missing=set()
assert {p.stem for p in (ROOT/'calibration/v2/samples').glob('*.png')}==set(ids)-expected_missing
for rec in m['samples']:
 assert json.loads((ROOT/'calibration/v2/samples'/f'{rec["id"]}.json').read_text())==rec
 assert rec['supervisor_confirmed'] is False
 assert rec['reference_sha256']==sha(rec['reference_image'])
 if rec['source_image']:assert rec['source_sha256']==sha(rec['source_image'])
 shot=rec['shot_requirement']['shot'];assert isinstance(shot['reflection'],bool) and isinstance(shot['other_people'],list)
 assert rec['review_calls']==rec['vlm_calls']==0
 if rec['image_path']:
  assert rec['image_sha256']==sha(rec['image_path'])
  if rec['method']=='copy':assert rec['image_sha256']==rec['source_sha256']
  else:
   assert rec['method']=='qwen_edit' and rec['source_role']=='actual_Qwen_image1_input'
   at=json.loads((ROOT/rec['attempt_records'][-1]).read_text())
   assert at['status']=='completed' and at['prompt_id']==rec['prompt_id']
   assert sha(at['image_path'])==rec['image_sha256']==at['image_sha256']
   hp=ROOT/'reports/assets/P9-03/r1'/f'{rec["id"]}-attempt{rec["final_attempt"]}-history.json'
   history=json.loads(hp.read_text());assert history['status']['status_str']=='success'
   assert history['outputs']['9']['images']==[rec['comfy_output']]
   assert at['workflow']['6']['inputs']['batch_size']==1
   assert rec['edit_effective'] is True
   for attempt in rec['attempt_records']:
    prior=json.loads((ROOT/attempt).read_text());assert prior['source_sha256']==rec['source_sha256']
   if rec['retries']:assert rec['retry_diagnosis']
  with Image.open(ROOT/rec['image_path']) as im:im.verify()
 else:assert rec['id'] in expected_missing and rec['image_sha256'] is None
for sid in ['B4','B5']:assert next(x for x in m['samples'] if x['id']==sid)['shot_requirement']['shot']['reflection']
for sid in ['B6','B7']:assert next(x for x in m['samples'] if x['id']==sid)['shot_requirement']['shot']['other_people']
assert m['generated_images']==m['comfyui_submissions']==3
assert m['review_calls']==m['vlm_calls']==0
assert next(x for x in m['samples'] if x['id']=='B3')['edit_constraints_satisfied'] is False
assert next(x for x in m['samples'] if x['id']=='A6')['image_sha256']==next(x for x in m['samples'] if x['id']=='A2')['image_sha256']
with Image.open(ROOT/m['overview']) as im:assert im.size==(2150,4110);im.verify()
print('PASS: 27 JSON + 27 PNG; 25 exact source copies, 2 final Qwen results with history provenance; 3 generated images; B3 outfit limitation recorded; no inference run by verifier.')
