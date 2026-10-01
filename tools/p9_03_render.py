"""Render saved samples only, no inference."""
import json
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1]
def load(p):return json.loads((ROOT/p).read_text())
m=load('calibration/v2/manifest.json');records=m['samples'];refs={}
for r in records:
 refs[r['task_root']]=r['reference_image']
# References reflect each actual task's locked outfit/identity.
refs={k:v for k,v in refs.items() if k in ['runs/p3_02_a_ipadapter', 'runs/p3_02_b_ipadapter', 'runs/p5_02_b_r1', 'jobs/cg-fc625e111558', 'jobs/cg-76d2eec57dfc']}
fontpath='/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc';font=ImageFont.truetype(fontpath,22);small=ImageFont.truetype(fontpath,18);big=ImageFont.truetype(fontpath,30)
w,h=430,540;sheet=Image.new('RGB',(w*5,120+540+70+2*h+70+2*h+70+2*h),'#f5f4f0');d=ImageDraw.Draw(sheet)
d.text((20,15),'P9-03 第二轮校准候选 | 声称真值，新样本待监工确认',font=big,fill='#172c3d');d.text((20, sixty:=65),'返工 1：B2/B3 新编辑、A6 指定背影；新样本待监工确认；未调用审核/视觉模型。',font=font,fill='#943020')
for i,(root,p) in enumerate(refs.items()):
 x=i*w;im=ImageOps.contain(Image.open(ROOT/p).convert('RGB'),(w-20,475));sheet.paste(im,(x+(w-im.width)//2,155));d.text((x+10,120),load(root+'/identity.json')['input_name']+' 定妆参考',font=font,fill='black')
ypos=660
for group,title in [('A','A 看不见与不存在'),('B','B 单主角计数'),('C','C 瞳色（仅瞳色真值）')]:
 d.rectangle((0,ypos,w*5,ypos+60),fill='#203747');d.text((15,ypos+12),title,font=big,fill='white');ypos+=70
 rows=[r for r in records if r['id'].startswith(group)]
 for i,r in enumerate(rows):
  x=i%5*w;y0=ypos+i//5*h;d.text((x+12,y0+8),r['id'],font=big,fill='#172c3d')
  claim=r['claim'];chunks=[];part=''
  for ch in claim:
   if d.textlength(part+ch,font=small)>w-24:chunks.append(part);part=ch
   else:part+=ch
  chunks.append(part);d.multiline_text((x+12,y0+48),'\n'.join(chunks),font=small,fill='#943020',spacing=3)
  if r['image_path']:
   im=ImageOps.contain(Image.open(ROOT/r['image_path']).convert('RGB'),(w-18,h-110));sheet.paste(im,(x+(w-im.width)//2,y0+100))
  else:d.text((x+20,y0+240),'缺图：'+('远端执行冲突' if r['status']=='blocked' else '未找到合适来源'),font=font,fill='#943020')
 ypos+=2*h
sheet.save(ROOT/'review/P9-03-samples.jpg',quality=95,subsampling=0)
