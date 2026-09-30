"""Compare completed CLI jobs; does not submit images or call models."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from storycg.state import save_json_atomic


def compare(output, roots, labels):
    roots=[Path(p) for p in roots]
    boards=[(p/'storyboard.json').read_bytes() for p in roots]
    manifests=[json.loads((p/'manifest.json').read_text()) for p in roots]
    if len(labels)!=len(roots) or len(roots)<2:
        raise ValueError('one label per condition, at least two conditions')
    if any(b!=boards[0] for b in boards):
        raise ValueError('storyboard differs between conditions')
    seeds=[[i['seed'] for i in m['images']] for m in manifests]
    if any(s!=seeds[0] for s in seeds):
        raise ValueError('seeds differ between conditions')
    portrait_hashes=[m['reference_relationship']['portrait']['image']['sha256'] for m in manifests]
    if len(set(portrait_hashes))!=1:
        raise ValueError('portrait differs between conditions')
    sheets=[Image.open(p/'contact_sheet.jpg').convert('RGB') for p in roots]
    if any(s.size!=sheets[0].size for s in sheets):
        raise ValueError('contact sheet dimensions differ')
    width,height=sheets[0].size;header=100
    canvas=Image.new('RGB',(width*len(sheets),height+header),'#f0f0f0')
    font=ImageFont.truetype('/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',38)
    draw=ImageDraw.Draw(canvas)
    cells=[]
    for col,(sheet,label,root,manifest) in enumerate(zip(sheets,labels,roots,manifests)):
        bbox=draw.textbbox((0,0),label,font=font)
        draw.text((col*width+(width-(bbox[2]-bbox[0]))/2,25),label,font=font,fill='#101010')
        canvas.paste(sheet,(col*width,header))
        cells.append({'column':col+1,'label':label,'job_dir':str(root),'seeds':seeds[col],
                      'manifest':str(root/'manifest.json'),'manifest_sha256':hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest(),
                      'contact_sheet':str(root/'contact_sheet.jpg'),'images':[i['path'] for i in manifest['images']]})
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise ValueError('comparison exists; will not overwrite prior evidence')
    canvas.save(output,'JPEG',quality=94,subsampling=0)
    save_json_atomic(output.with_suffix('.json'),{'image':str(output),'storyboard_sha256':hashlib.sha256(boards[0]).hexdigest(),
        'portrait_sha256':portrait_hashes[0],'columns':cells})
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--jobs',nargs='+',required=True,type=Path)
    parser.add_argument('--labels',nargs='+',required=True)
    args=parser.parse_args()
    print(compare(args.output,args.jobs,args.labels))
