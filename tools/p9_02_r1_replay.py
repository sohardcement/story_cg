import json,re,time
from PIL import Image,ImageDraw,ImageOps
from storycg.detect import ROOT
from storycg.detection_review import audit_detection,settings
from storycg.repairs import mask_boundary_check
out=ROOT/'reports/assets/P9-02-r1';old=json.loads((ROOT/'reports/assets/P9-02-replay/results.json').read_text())['results']
manifest=json.loads((ROOT/'calibration/manifest.json').read_text());spec=json.loads((ROOT/'calibration/spec.json').read_text())
inputs=[(r['id'],ROOT/r['image'],ROOT/r['provenance']['identity_file'],ROOT/r['reference_image'] if r['reference_image'] else None) for r in old]
for name in ['E3a','E3b','C2','C5']:
 s=next(s for s in manifest['samples'] if s['id']==name);inputs.append((name,ROOT/'calibration/samples'/f'{name}.png',(ROOT/s['source_image']).parent/'identity.json',ROOT/spec['references'][s['character_group']]))
rows=[];tiles=[]
for name,path,identitypath,ref in inputs:
 identity=json.loads(identitypath.read_text());audit=audit_detection(path,identity,{},reference_path=ref);eye=audit['eye_color_sampled'];rows.append({'id':name,'image':str(path.relative_to(ROOT)),'reference':str(ref.relative_to(ROOT)) if ref else None,'audit':audit})
 image=Image.open(path).convert('RGB');draw=ImageDraw.Draw(image)
 for key,color in [('roi','yellow'),('hair_regions','cyan'),('skin_regions','orange')]:
  regions=[eye[key]] if key=='roi' and key in eye else eye.get(key,[])
  for b in regions:draw.rectangle(b,outline=color,width=2)
 for x,y in eye.get('sampled_pixels',[]):draw.point((x,y),fill='magenta')
 tile=Image.new('RGB',(600,420),'#20242b');thumb=ImageOps.contain(image,(400,360));tile.paste(thumb,(0,50))
 face=max(audit['detection']['faces'],key=lambda f:f['area_ratio'],default=None)
 if face:
  crop=image.crop(face['bbox']);crop=ImageOps.contain(crop,(190,350));tile.paste(crop,(405,60))
 ImageDraw.Draw(tile).text((10,10),f"{name} {eye['status']} H={eye.get('hue')} n={eye['pixel_count']}",fill='white')
 tile.save(out/f'{name}-eyes.jpg',quality=95);tiles.append(tile);print(name,eye['status'],eye.get('hue'),eye['pixel_count'],flush=True)
sheet=Image.new('RGB',(1800,2520),'#20242b')
for i,tile in enumerate(tiles):sheet.paste(tile,((i%3)*600,(i//3)*420))
sheet.save(ROOT/'review/P9-02-eyes.jpg',quality=95)
seams=[]
for name in ['p5_01_a_ipadapter_shot4','p9_02_face']:
 root=ROOT/'runs'/name;rec=list(json.loads((root/'state.json').read_text())['repairs'].values())[-1]
 with Image.open(root/rec['source_image']['path']) as a,Image.open(root/rec['output']['path']) as b,Image.open(root/rec['region']['mask']) as m:seams.append({'job':name,**mask_boundary_check(a,b,m)})
(out/'replay.json').write_text(json.dumps({'config':settings(),'results':rows,'seams':seams,'vision_calls':0,'images':0},ensure_ascii=False,indent=2)+'\n')
