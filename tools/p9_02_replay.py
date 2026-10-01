"""Serial fresh local replay of the supervisor's 14 read-only samples."""
import hashlib
import json
from pathlib import Path
import re
import time
from PIL import Image
from storycg.detection_review import audit_detection, blind_thumbnails, settings
from storycg.detect import ROOT


def main():
    started=time.monotonic();data=json.loads((ROOT/'calibration/detect/results.json').read_text())
    calibration=json.loads((ROOT/'calibration/manifest.json').read_text())
    spec=json.loads((ROOT/'calibration/spec.json').read_text())
    out=ROOT/'reports/assets/P9-02-replay';out.mkdir(parents=True,exist_ok=True)
    results=[]
    for old in data['results']:
        path=ROOT/old['image'];parent=path.parent;entry={};identity={};reference=None
        provenance={}
        if (parent/'identity.json').exists():
            identity=json.loads((parent/'identity.json').read_text())
            number=int(re.search(r'_shot(\d+)_',path.name).group(1))
            board=json.loads((parent/'storyboard.json').read_text());entry=board['shots'][number-1]
            provenance={'identity_file':str((parent/'identity.json').relative_to(ROOT)),
                        'storyboard_file':str((parent/'storyboard.json').relative_to(ROOT)),'shot':number}
            if (parent/'portrait.json').exists():
                portrait=json.loads((parent/'portrait.json').read_text());reference=parent/portrait['image']['path']
        else:
            sample=next(s for s in calibration['samples'] if s['id']==path.stem)
            src=ROOT/sample['source_image'];identity=json.loads((src.parent/'identity.json').read_text())
            entry=sample['shot_requirement'];reference=ROOT/spec['references'][sample['character_group']]
            provenance={'identity_file':str((src.parent/'identity.json').relative_to(ROOT)),
                        'entry_source':'calibration/manifest.json:'+sample['id']}
        # The supervisor explicitly labels these inputs as reflections. Replay a
        # declared variant in a NEW record; do not rewrite legacy storyboard files.
        declaration=old['id'] in ('D3','D10','D11')
        declared={**entry,'shot':{**entry.get('shot',{}),**({'reflection':True} if declaration else {})}}
        audit=audit_detection(path,identity,declared,reference_path=reference)
        if declaration:
            from storycg.detection_review import protagonist_count
            d=audit['detection'];audit['legacy_count']=protagonist_count(d['faces'],d['persons'],entry.get('shot',{}))
        _,thumb=blind_thumbnails([path],out/old['id']/'blind')
        row={'id':old['id'],'image':old['image'],'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
             'reference_image':str(reference.relative_to(ROOT)) if reference else None,
             'reference_sha256':hashlib.sha256(reference.read_bytes()).hexdigest() if reference else None,
             'provenance':provenance,'supervisor_reflection_declaration':declaration,
             'blind_thumbnail':thumb[0],**audit}
        results.append(row)
        print(old['id'],audit['protagonist_count']['protagonist_count'],audit['protagonist_count']['status'],
              audit['eye_color_sampled']['status'],audit['accessory_color_sampled']['status'],flush=True)
    document={'config':settings(),'results':results,'generated_images':0,'vision_calls':0,
              'elapsed_seconds':time.monotonic()-started}
    (out/'results.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
