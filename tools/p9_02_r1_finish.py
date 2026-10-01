import ast,json,subprocess,hashlib
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
from storycg.detect import ROOT
out=ROOT/'reports/assets/P9-02-r1';r=json.loads((out/'replay.json').read_text());live=json.loads((out/'live.json').read_text())
old=ast.parse(subprocess.check_output(['git','show','HEAD:src/storycg/review.py'],text=True));new=ast.parse((ROOT/'src/storycg/review.py').read_text())
for name in ['audit_single','audit_consistency','load_reliability','annotate_reliability','enforce_visibility','identity_checklist','grading_schema']:
 a=next(n for n in old.body if isinstance(n,ast.FunctionDef) and n.name==name);b=next(n for n in new.body if isinstance(n,ast.FunctionDef) and n.name==name);assert ast.dump(a)==ast.dump(b)
assert (ROOT/'config/review_reliability.json').read_bytes()==subprocess.check_output(['git','show','HEAD:config/review_reliability.json'])
subprocess.run(['git','diff','--check'],check=True)
sheet=Image.new('RGB',(1440,560),'#20242b')
paths=[ROOT/'jobs/cg-76d2eec57dfc/storycg_cg-76d2eec57dfc_shot01_a001.png',ROOT/'runs/p9_02_r1_duplicate'/live['results'][0]['output']['path']]
for i,path in enumerate(paths):
 im=ImageOps.contain(Image.open(path).convert('RGB'),(710,510));sheet.paste(im,(i*720,45));ImageDraw.Draw(sheet).text((i*720+10,10),'BEFORE' if i==0 else 'QWEN EDIT / count: none_visible',fill='white')
sheet.save(ROOT/'review/P9-02-r1-duplicate.jpg',quality=95)
(out/'frozen.json').write_text(json.dumps({'frozen_review_unchanged':True,'reliability_unchanged':True,'diff_check':True,'source_sha256':hashlib.sha256(paths[0].read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(paths[1].read_bytes()).hexdigest()},indent=2)+'\n')
