"""Offline integrity checks; no image generation or reviewer requests."""
import argparse,json,hashlib
from pathlib import Path
from PIL import Image,ImageChops
from storycg.reference import digest
p=argparse.ArgumentParser();p.add_argument('cases');args=p.parse_args();summaries=[]
for case in json.loads(Path(args.cases).read_text()):
 root=Path(case['destination']);state=json.loads((root/'state.json').read_text());source=Path(case['source']);original=json.loads((source/'state.json').read_text())
 for a in original['attempts']:
  if a['status']=='completed':
   assert digest(source/a['image']['path'])==a['image']['sha256']
   assert digest(root/a['image']['path'])==a['image']['sha256']
 rec=state['repairs'][f"shot{case['shot']:02d}_{case.get('repair_id','repair1')}"]
 stages=[a for a in state['attempts'] if a.get('repair_key')==f"shot{case['shot']:02d}_{case.get('repair_id','repair1')}"]
 for a in stages:
  assert root.name in a['filename_prefix'] and a['shot_id'] in a['filename_prefix']
  assert all(a['repair_diagnosis'].get(k) for k in ['diagnosis','hypothesis','changes','result'])
  if a['status']=='completed':assert digest(root/a['image']['path'])==a['image']['sha256']
 if 'output' in rec and 'region' in rec:
  with Image.open(root/rec['source_image']['path']).convert('RGB') as before, Image.open(root/rec['output']['path']).convert('RGB') as after:
   assert before.size==after.size
   diff=ImageChops.difference(before,after);box=diff.getbbox();region=rec['region']['pixels']
   assert box and box[0]>=region[0] and box[1]>=region[1] and box[2]<=region[2] and box[3]<=region[3]
   assert digest(root/rec['region']['mask'])==rec['region']['mask_sha256']
   assert digest(root/rec['region']['input']['path'])==rec['region']['input']['sha256']
 reportpath=root/'repair_review_frozen.json'
 if reportpath.exists():
  review=json.loads(reportpath.read_text());assert review['config']['framing_definitions']
  assert review['image']['sha256']==rec['output']['sha256']
  expected={n for n in [case['shot']-1,case['shot']+1] if 1<=n<=len(json.loads((root/'storyboard.json').read_text())['shots'])}
  assert {n['shot'] for n in review['neighbors']}==expected
 requests=0
 if (root/'review_log.jsonl').exists():requests=sum(json.loads(line)['event']=='request' for line in (root/'review_log.jsonl').read_text().splitlines())
 summaries.append({'job':root.name,'completed_new_images':sum(a['status']=='completed' for a in state['attempts'] if not a.get('inherited')),'submission_attempts':sum(a.get('post_count',0) for a in state['attempts'] if not a.get('inherited')),'visual_posts':requests,'repair_status':rec['status'],'output':rec.get('output'),'review':json.loads(reportpath.read_text()).get('conclusion') if reportpath.exists() else None})
print(json.dumps({'cases':summaries,'completed_new_images':sum(s['completed_new_images'] for s in summaries),'submission_attempts':sum(s['submission_attempts'] for s in summaries),'visual_posts':sum(s['visual_posts'] for s in summaries)},ensure_ascii=False,indent=2))
