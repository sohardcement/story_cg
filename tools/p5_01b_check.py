"""Offline hashes, frozen config and honest inherited-generation accounting."""
import json
from pathlib import Path
from PIL import Image,ImageChops,ImageDraw
from storycg.reference import digest
from storycg.config import load_config
cases=json.loads(Path('reports/assets/P5-01b/cases.json').read_text());out=[]
for c in cases:
 root=Path(c['destination']);state=json.loads((root/'state.json').read_text());key=f"shot{c['shot']:02d}_{c.get('repair_id','repair1')}";rec=state['repairs'][key]
 assert digest(root/rec['source_image']['path'])==rec['source_image']['sha256']
 assert digest(root/rec['output']['path'])==rec['output']['sha256']
 name='repair_review_b_frozen.json' if c.get('reused_previous') else 'repair_review_frozen.json'
 review=json.loads((root/name).read_text());assert review['config']==load_config('config/default.json');assert review['image']['sha256']==rec['output']['sha256']
 stages=[a for a in state['attempts'] if not a.get('inherited')];prefixes=[a['filename_prefix'] for a in stages];assert len(prefixes)==len(set(prefixes))
 outside=None
 if c['primitive']=='face_detail':
  with Image.open(root/rec['source_image']['path']) as base,Image.open(root/rec['output']['path']) as output:
   mask=Image.new('L',base.size,255);p=rec['region']['pixels'];ImageDraw.Draw(mask).rectangle((p[0],p[1],p[2]-1,p[3]-1),fill=0)
   outside=ImageChops.multiply(ImageChops.difference(base.convert('RGB'),output.convert('RGB')),mask.convert('RGB')).getbbox() is None
   assert outside
 out.append({'root':str(root),'output_hash_matches':True,'source_hash_matches':True,'frozen_config_matches':True,'outside_unchanged':outside,'post_count_total_in_copied_state':sum(a.get('post_count',0) for a in stages),'generated_stages_total':len(stages)})
Path('reports/assets/P5-01b/integrity.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
