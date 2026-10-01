"""Compose complete old/new contact sheets, without cropping or altering artwork."""
import argparse
from PIL import Image,ImageDraw,ImageFont
p=argparse.ArgumentParser()
p.add_argument('old');p.add_argument('new');p.add_argument('output')
p.add_argument('--old-label',required=True);p.add_argument('--new-label',required=True)
a=p.parse_args()
width=1100;header=72;gap=20
panels=[]
for path in (a.old,a.new):
    im=Image.open(path).convert('RGB');im=im.resize((width,round(im.height*width/im.width)),Image.Resampling.LANCZOS);panels.append(im)
out=Image.new('RGB',(width*2+gap,max(x.height for x in panels)+header),'#eeeeee')
d=ImageDraw.Draw(out);font=ImageFont.truetype('/usr/share/fonts/ttf-ia-writer/iAWriterQuattroS-Regular.ttf',22)
for i,(im,label) in enumerate(zip(panels,(a.old_label,a.new_label))):
    x=i*(width+gap);out.paste(im,(x,header));d.text((x+18,20),label,fill='black',font=font)
out.save(a.output,quality=95)
print(a.output)
