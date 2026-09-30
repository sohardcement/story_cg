"""Render every current probe and all its references, without selecting outcomes."""
import json
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont,ImageOps
font_path='/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc'
font=ImageFont.truetype(font_path,24);title_font=ImageFont.truetype(font_path,32)
for path in sorted(Path('runs/p6_01b').glob('*/identity.json')):
 identity=json.loads(path.read_text());evidence=identity['tier_evidence']
 if not evidence.get('test_images'):continue
 root=path.parent/evidence['artifact_root']
 tests=evidence['test_images'];refs=[r for r in evidence['references'] if r['status']=='downloaded']
 lines=[]
 for row in evidence['reliable']:
  statuses='/'.join(c['status'] for c in row['comparisons'])
  lines.append(f"{row['tag']}: {'通过' if row['passed'] else '未通过'} ({statuses})")
 for row in evidence.get('outfit_comparisons',[]):
  statuses='/'.join('不适用' if c['status']=='not_applicable' else c['status'] for c in row['comparisons'])
  lines.append(f"服装仅诊断 {row['tag']}: {statuses}")
 notes=[];line=[]
 for row in evidence['unverified']:
  candidate=', '.join(line+[row['tag']])
  if len(candidate)>85 and line:
   notes.append('未可靠审核：'+', '.join(line));line=[]
  line.append(row['tag'])
 if line:notes.append('未可靠审核：'+', '.join(line))
 width=2000;top=90;height=top+860+45+35*(len(lines)+len(notes)+1)
 sheet=Image.new('RGB',(width,height),'#eeeeee');draw=ImageDraw.Draw(sheet)
 draw.text((25,15),f"{identity['input_name']} | {identity['identity_tier']} | 左：既有测试图（本轮复用）；右：筛选后的 Danbooru 参考",font=title_font,fill='black')
 panels=[(tests[0],'测试图',0,680)]+[(r,f"参考 post {r['post_id']} / score {r['score']}",680+i*440,440) for i,r in enumerate(refs)]
 for record,label,x,cell_width in panels:
  with Image.open(root/record['path']) as image:
   thumb=ImageOps.contain(image.convert('RGB'),(cell_width-24,800))
  sheet.paste(thumb,(x+(cell_width-thumb.width)//2,top+(800-thumb.height)//2))
  draw.text((x+15,top+815),label,font=font,fill='black')
 y=top+880
 for line in lines+notes+['层级仅采信可靠外貌项；服装仅作诊断。完整角色身份仍需监工看图。']:
  draw.text((25,y),line,font=font,fill='black');y+=35
 target=Path('review')/f"P6-01b-{identity['input_name']}.jpg"
 sheet.save(target,'JPEG',quality=92,subsampling=0)
 print(target)
