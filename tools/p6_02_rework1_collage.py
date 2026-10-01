"""Create review sheets from persisted fresh probes and L3 attempt records."""
import json,hashlib
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
from storycg.state import save_json_atomic
assets=Path('reports/assets/P6-02-rework1');ledger=json.loads((assets/'usage.json').read_text());manifest=[]
for row in ledger['results']:
 job=Path(row['job']);identity=json.loads((job/'identity.json').read_text());e=json.loads((job/'knowledge-result.json').read_text())['tier_evidence']
 probe=job/e['artifact_root'];test=probe/e['test_images'][0]['path'];refs=[r for r in e['references'] if r.get('status')=='downloaded']
 panels=[('Knowledge probe (L1)' if identity.get('lora') else 'Knowledge probe (L2)',test),('Selected reference (not design evidence)',probe/refs[0]['path'])]
 if str(job) in ledger['selected_l3']:
  paths=list(job.glob('l3/*/attempts.json'));assert len(paths)==1
  attempts=json.loads(paths[0].read_text());portrait=job/'portrait.json'
  if portrait.exists():
   record=json.loads(portrait.read_text());im=record['image'];label='L3 approved portrait'
  else:im=attempts[-1]['image'];label='L3 final attempt FAILED / L4'
  panels.append((label,job/im['path']))
 sheet=Image.new('RGB',(600*len(panels),850),'#eeeeee');draw=ImageDraw.Draw(sheet)
 records=[]
 for n,(label,path) in enumerate(panels):
  with Image.open(path) as im:pic=ImageOps.contain(im.convert('RGB'),(590,800))
  sheet.paste(pic,(n*600+(600-pic.width)//2,40+(800-pic.height)//2));draw.text((n*600+12,12),label,fill='black')
  records.append({'label':label,'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
 tag=identity['character_tag'].replace('/','_');prefix='P6-02-L3-' if len(panels)==3 else 'P6-02-rework1-probe-'
 path=Path('review')/(prefix+tag+'.jpg');sheet.save(path,quality=94)
 manifest.append({'tag':tag,'tier':identity['identity_tier'],'path':str(path),'panels':records})
save_json_atomic(assets/'collages.json',manifest)
print(json.dumps(manifest,ensure_ascii=False,indent=2))
