"""Render every actual repair pair and a requested baseline/current comparison."""
import argparse,json
from pathlib import Path
from PIL import Image,ImageDraw,ImageOps,ImageFont


def font(size):
    for name in ('LiberationSans-Regular.ttf','LiberationSans-Bold.ttf','DejaVuSans.ttf'):
        try:return ImageFont.truetype(name,size)
        except OSError:pass
    raise OSError('no scalable font available')


def compare(baseline,current,output):
    with Image.open(baseline) as a,Image.open(current) as b:
        a=a.convert('RGB');b=b.convert('RGB');w=max(a.width,b.width);h=max(a.height,b.height)
        canvas=Image.new('RGB',(w*2+20,h+60),'#eeeeee')
        canvas.paste(ImageOps.contain(a,(w,h)),(0,60));canvas.paste(ImageOps.contain(b,(w,h)),(w+20,60))
    draw=ImageDraw.Draw(canvas);draw.text((20,10),'P3-02 / IPAdapter 0.4',font=font(30),fill='black');draw.text((w+40,10),'P5-02 / automatic cycle',font=font(30),fill='black')
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);canvas.save(output,quality=94,subsampling=0)
    return output


def repair_pairs(root,output):
    root=Path(root);state=json.loads((root/'state.json').read_text());rows=[]
    for key,rec in state.get('repairs',{}).items():
        if rec.get('output'):rows.append((key,rec))
    if not rows:return None
    canvas=Image.new('RGB',(1240,len(rows)*540),'#eeeeee');draw=ImageDraw.Draw(canvas)
    for i,(key,rec) in enumerate(rows):
        draw.text((15,i*540+5),key+' / '+rec['request']['primitive'],font=font(24),fill='black')
        for column,item in enumerate([rec['source_image'],rec['output']]):
            with Image.open(root/item['path']) as im:thumb=ImageOps.contain(im.convert('RGB'),(600,495))
            canvas.paste(thumb,(column*620+(620-thumb.width)//2,i*540+40+(495-thumb.height)//2))
    Path(output).parent.mkdir(parents=True,exist_ok=True);canvas.save(output,quality=94,subsampling=0);return Path(output)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('job',type=Path);p.add_argument('--baseline',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--repairs',type=Path)
    a=p.parse_args();print(compare(a.baseline,a.job/'contact_sheet.jpg',a.output))
    if a.repairs:print(repair_pairs(a.job,a.repairs))
