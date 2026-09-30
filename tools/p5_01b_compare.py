import json
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw,ImageFont
cases=json.loads(Path('reports/assets/P5-01b/cases.json').read_text());canvas=Image.new('RGB',(2100,3900),'#eeeeee');d=ImageDraw.Draw(canvas);font=ImageFont.truetype('LiberationSans-Regular.ttf',22)
for i,c in enumerate(cases):
 old=Path(c['previous']);prev=json.loads((old/'state.json').read_text());key=f"shot{c['shot']:02d}_{c.get('repair_id','repair1')}";pr=prev['repairs'][key]
 root=Path(c['destination']);state=json.loads((root/'state.json').read_text());rec=state.get('repairs',{}).get(key,{})
 d.text((12,i*780+5),f"{i+1}: {c['primitive']} | {root.name}",font=font,fill='black')
 for j,(folder,im,label) in enumerate([(old,pr['source_image'],'ORIGINAL'),(old,pr.get('output'),'P5-01'),(old if c.get('reused_previous') else root,pr.get('output') if c.get('reused_previous') else rec.get('output'),'P5-01b (retained)' if c.get('reused_previous') else 'P5-01b')]):
  d.text((j*700+15,i*780+35),label,font=font,fill='black')
  if im:
   img=Image.open(folder/im['path']).convert('RGB');thumb=ImageOps.contain(img,(680,690));canvas.paste(thumb,(j*700+(700-thumb.width)//2,i*780+75))
   if c['primitive']=='face_detail':
    box=pr['region']['pixels'];zoom=ImageOps.contain(img.crop(box),(240,210));canvas.paste(zoom,(j*700+445,i*780+555));d.rectangle((j*700+443,i*780+553,j*700+447+zoom.width,i*780+557+zoom.height),outline='red',width=2)
  else:d.text((j*700+20,i*780+300),'NOT COMPLETED',font=font,fill='red')
canvas.save('review/P5-01b-repairs.jpg',quality=93,subsampling=0)
canvas.resize((1050,1950)).save('reports/assets/P5-01b/overview.jpg')
