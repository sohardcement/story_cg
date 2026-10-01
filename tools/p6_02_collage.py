"""Build review sheets solely from this order's persisted real image records."""
import json
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-02');ledger=json.loads((root/'live-usage.json').read_text());manifest=[]
for row in {r['tag']:r for r in ledger['results']}.values():
 job=Path(row['job']);identity=json.loads((job/'identity.json').read_text());e=identity.get('tier_evidence',{})
 if not e.get('test_images'):continue
 probe=job/e['artifact_root'];test=probe/e['test_images'][0]['path'];refs=[r for r in e['references'] if r.get('status')=='downloaded']
 panels=[('L2 test',test),('Selected reference',probe/refs[0]['path'])]
 if identity['identity_tier']=='L3':
  portrait=json.loads((job/'portrait.json').read_text());panels.append(('L3 portrait',job/portrait['image']['path']))
 elif identity['identity_tier']=='L4':
  attempts=list(job.glob('l3/*/attempts.json'))
  if attempts:
   data=json.loads(max(attempts,key=lambda p:p.stat().st_mtime).read_text())
   if data:panels.append(('L3 final attempt (failed)',job/data[-1]['image']['path']))
 sheet=Image.new('RGB',(600*len(panels),850),'#eeeeee');draw=ImageDraw.Draw(sheet)
 for n,(label,path) in enumerate(panels):
  with Image.open(path) as im:pic=ImageOps.contain(im.convert('RGB'),(590,800))
  sheet.paste(pic,(n*600+(600-pic.width)//2,40+(800-pic.height)//2));draw.text((n*600+12,12),label,fill='black')
 tag=identity['character_tag'].replace('/','_');prefix='P6-02-L3-' if len(panels)==3 else 'P6-02-L2-'
 path=Path('review')/(prefix+tag+'.jpg');sheet.save(path,quality=94)
 manifest.append({'tag':tag,'tier':identity['identity_tier'],'path':str(path),'panels':[{'label':label,'path':str(p)} for label,p in panels]})
save_json_atomic(root/'collages.json',manifest)
print(json.dumps(manifest,ensure_ascii=False,indent=2))
