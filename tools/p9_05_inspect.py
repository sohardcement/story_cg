"""Read-only evidence collection and explicitly labelled before/after layouts."""
import hashlib
import json
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
from storycg.budget import count_usage

ROOT=Path(__file__).resolve().parents[1]
SPEC=ROOT/'reports/assets/P9-05/regressions.json'


def read(root,name,default=None):
    p=root/name
    return json.loads(p.read_text()) if p.exists() else (default if default is not None else {})


def write_new(path,data):
    if path.exists() and path.read_bytes()!=data:raise ValueError('refuse to overwrite differing artifact: '+str(path))
    if not path.exists():path.write_bytes(data)


def inspect(spec):
    root=ROOT/spec['job'];identity=read(root,'identity.json');cycle=read(root,'cycle.json')
    if not root.exists():return {'spec':spec,'status':'not_started'}
    report=cycle.get('final_review',read(root,'review.json'))
    data={'spec':spec,'agent':read(root,'agent.json'),'identity_tier':identity.get('identity_tier'),
        'tier_evidence':identity.get('tier_evidence'), 'usage':count_usage(root),
        'storyboard':read(root,'storyboard.json'), 'portrait':read(root,'portrait.json'),
        'portrait_attempts':read(root,'portrait_attempts.json',[]),
        'review_gate':read(root,'review_gate.json'),'detection_items':report.get('detection_items'),
        'narrative_blind':report.get('narrative_blind'),'narrative_compare':report.get('narrative_compare'),
        'raw_conclusion':report.get('conclusion'),'layer_conclusions':report.get('layer_conclusions'),
        'cycle_phase':cycle.get('phase'),'final_review_skipped':cycle.get('final_review_skipped'),
        'blocked':cycle.get('blocked'),'repair_stops':{n:s.get('stop') for n,s in cycle.get('shots',{}).items()},
        'manifest':str(root/'manifest.json') if (root/'manifest.json').exists() else None}
    preview=root/'contact_sheet.jpg'
    if preview.exists():
        new=ROOT/'review'/f"{spec['name']}-{spec['short']}-v2.jpg"
        write_new(new,preview.read_bytes())
        data['preview']={'path':str(new),'sha256':hashlib.sha256(new.read_bytes()).hexdigest()}
    panels=[]
    for title,path,status in [('OLD',ROOT/spec['old'] if spec['old'] else None,spec['old_status']),
                              ('NEW',preview if preview.exists() else None,data['agent'].get('phase','unknown'))]:
        panel=Image.new('RGB',(850,780),'#e8e8e8');draw=ImageDraw.Draw(panel)
        draw.text((20,15),title+' | '+status,fill='black')
        if path and path.exists():
            with Image.open(path) as im:thumb=ImageOps.contain(im.convert('RGB'),(850,710))
            panel.paste(thumb,((850-thumb.width)//2,55+(710-thumb.height)//2))
        else:draw.text((30,350),'NO STORY PREVIEW: no story images available',fill='black')
        panels.append(panel)
    sheet=Image.new('RGB',(1700,780),'white');sheet.paste(panels[0],(0,0));sheet.paste(panels[1],(850,0))
    import io
    buffer=io.BytesIO();sheet.save(buffer,'JPEG',quality=92,subsampling=0)
    compare=ROOT/'review'/f"P9-05-{spec['name']}-前后.jpg";write_new(compare,buffer.getvalue())
    data['comparison']={'path':str(compare),'sha256':hashlib.sha256(compare.read_bytes()).hexdigest()}
    return data


if __name__=='__main__':
    specs=json.loads(SPEC.read_text());data=[inspect(s) for s in specs]
    path=ROOT/'reports/assets/P9-05/final-evidence.json'
    write_new(path,json.dumps(data,ensure_ascii=False,indent=2).encode())
    print(json.dumps([{k:r.get(k) for k in ('spec','identity_tier','usage','raw_conclusion','review_gate','preview','comparison')} for r in data],ensure_ascii=False,indent=2))
