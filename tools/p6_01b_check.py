"""Read-only verification of probe provenance, workflow routing and usage."""
import hashlib,json
from pathlib import Path
from storycg.state import save_json_atomic
records=[];requests=0;submissions=0
for path in sorted(Path('runs/p6_01b').glob('*/identity.json')):
 identity=json.loads(path.read_text());evidence=identity['tier_evidence'];root=path.parent/evidence['artifact_root']
 state=json.loads((root/'state.json').read_text())
 assert len(state['attempts'])==1 and state['stats']['submission_attempts']==1
 submissions+=state['stats']['submission_attempts']
 test=evidence['test_images'][0];graph=json.loads((root/test['workflow_path']).read_text())
 assert graph==state['attempts'][0]['workflow']
 assert test['prompt_id']==state['attempts'][0]['prompt_id']
 assert hashlib.sha256((root/test['path']).read_bytes()).hexdigest()==test['sha256']
 assert all(n['class_type'] in {'CheckpointLoaderSimple','LoraLoader','CLIPTextEncode','EmptyLatentImage','KSampler','VAEDecode','SaveImage'} for n in graph.values())
 loaded=[n['inputs']['lora_name'] for n in graph.values() if n['class_type']=='LoraLoader']
 style=[l['name'] for l in evidence['generation_config']['style_loras']]
 lora=identity.get('lora')
 assert sorted(loaded)==sorted(style+([lora['name']] if lora else []))
 for ref in evidence['references']:
  assert ref['rating']=='g' and ref['score']>=20
  assert hashlib.sha256((root/ref['path']).read_bytes()).hexdigest()==ref['sha256']
 logs=[json.loads(line) for line in (root/'review_log.jsonl').read_text().splitlines()]
 actual=sum(line.get('event')=='request' and line.get('purpose','').startswith('tier_v2_') for line in logs);requests+=actual
 assert actual==3 and len(evidence['comparisons'])==3
 for line in logs:
  if line.get('event')!='request' or not line.get('purpose','').startswith('tier_v2_'):continue
  content=line['request']['messages'][0]['content']
  images=[part['image_url']['url'] for part in content if part['type']=='image_url']
  assert len(images)==2 and images[0]['path']==test['path'] and images[0]['sha256']==test['sha256']
  texts=' '.join(part['text'] for part in content if part['type']=='text')
  assert 'Image 1 = generated test image' in texts and 'Image 2 = Danbooru reference' in texts
  schema=line['request']['response_format']['json_schema']['schema']
  assert all({'test_observation','reference_observation'}<=set(grade['required']) for grade in schema['properties'].values())
 assert evidence['tier_scope']=='appearance_only' and evidence['comparison_version']==2
 assert all(row['item']!='outfit' for row in evidence['reliable'])
 assert all(pair['image_roles']['Image 1']=='generated test image' for pair in evidence['comparisons'])
 for pair in evidence['comparisons']:
  for row in pair['items']:
   assert row['test_observation'] and row['reference_observation']
   if row['item']=='outfit' and not pair['outfit_applicable']:assert row['status']=='not_applicable'
 from storycg.tiers import select_reference_posts
 from storycg.identity_enrichment import load_rules
 directory=root/load_rules()['references']['directory']
 raw=json.loads((directory/'candidate-responses.json').read_text())
 selected,audit=select_reference_posts(identity,[post for page in raw for post in page['response']])
 assert [p['id'] for p in selected]==[r['post_id'] for r in evidence['references']]
 assert audit==json.loads((directory/'selection.json').read_text())
 before=json.loads(Path(evidence['previous_evidence_path']).read_text())
 assert evidence['test_images']==before['tier_evidence']['test_images']
 assert all(x['note']=='未经可靠审核' for x in evidence['unverified'])
 source=json.loads((Path('runs/p6_01a_rework1_resolve')/str(int(path.parent.name.rsplit('_',1)[1])).zfill(2)/'identity.json').read_text())
 for key in ('identity_block','appearance_tags','outfit_block','default_outfit_tags'):assert identity[key]==source[key]
 collage=Path('review')/f"P6-01b-{identity['input_name']}.jpg";assert collage.is_file()
 records.append({'character':identity['input_name'],'tier':identity['identity_tier'],'prompt_id':test['prompt_id'],
                 'test_path':str(root/test['path']),'workflow_path':str(root/test['workflow_path']),
                 'manifest':str(root/'manifest.json'),'collage':str(collage),'post_ids':[r['post_id'] for r in evidence['references']],
                 'loaded_loras':loaded,'vision_requests':actual})
assert len(records)==4 and submissions==4 and requests==12
freeze=json.loads(Path('calibration/reviewer_freeze.json').read_text())
assert all(hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest for name,digest in freeze['files'].items())
import subprocess
reliability=Path('config/review_reliability.json')
assert reliability.read_bytes()==subprocess.run(['git','show','HEAD:config/review_reliability.json'],check=True,capture_output=True).stdout
result={'frozen_files_verified':list(freeze['files']),'reliability_unchanged':True,'records':records,'usage':{'generated_images':4,'submissions':submissions,'vision_requests':requests}}
save_json_atomic('reports/assets/P6-01b-rework1/artifact-verification.json',result)
save_json_atomic('runs/p6_01b/manifest.json',result)
print(json.dumps(result,ensure_ascii=False,indent=2))
