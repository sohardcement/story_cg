"""Produce honest before/after rows, with an explicit missing-output panel."""
import argparse,json
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw,ImageFont
p=argparse.ArgumentParser();p.add_argument('cases');p.add_argument('--output',default='review/P5-01-repairs.jpg');args=p.parse_args()
cases=json.loads(Path(args.cases).read_text());w,h=1800,900;canvas=Image.new('RGB',(w,h*len(cases)),'#eeeeee');d=ImageDraw.Draw(canvas)
font=ImageFont.truetype('LiberationSans-Regular.ttf',24)
labels=['inpaint: object count / open-close states','inpaint: held object vs placed object','face detail: distant yellow eyes -> locked color','face detail: blue eyes -> locked color','relayout: obscured protagonist / object on head / stray hand']
for i,(c,label) in enumerate(zip(cases,labels)):
 root=Path(c['destination']);state=json.loads((root/'state.json').read_text());key=f"shot{c['shot']:02d}_{c.get('repair_id','repair1')}";rec=state['repairs'][key]
 d.text((20,i*h+10),label+' | '+root.name,font=font,fill='black')
 for j,k in enumerate(['source_image','output']):
  d.text((j*900+20,i*h+48),'BEFORE' if j==0 else 'AFTER',font=font,fill='black')
  if k not in rec:
   d.text((j*900+30,i*h+300),'NOT COMPLETED: ComfyUI unavailable',font=font,fill='red');continue
  im=Image.open(root/rec[k]['path']).convert('RGB');thumb=ImageOps.contain(im,(870,760));canvas.paste(thumb,(j*900+(900-thumb.width)//2,i*h+100))
  if c['primitive']=='face_detail':
   pixels=rec['region']['pixels'];zoom=ImageOps.contain(im.crop(pixels),(270,240));x=j*900+610;y=i*h+640;canvas.paste(zoom,(x,y));d.rectangle((x-3,y-3,x+zoom.width+3,y+zoom.height+3),outline='red',width=3)
Path(args.output).parent.mkdir(parents=True,exist_ok=True);canvas.save(args.output,quality=94,subsampling=0)
