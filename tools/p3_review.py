"""Review each comparison grid cell against the approved portrait, without changing grades."""
import json
import sys
import time
from pathlib import Path
from storycg.reference import require_portrait, digest
from storycg.review import VisionClient, messages, grading_schema, BBOX_SCHEMA, identity_checklist, crop_face, face_pair, conclude, result_statuses
from storycg.state import save_json_atomic
from p3_pilot import manifest


def run(root):
    root=Path(root).resolve();portrait=require_portrait(root)
    spec=json.loads((root/'pilot.json').read_text());cfg=json.loads((root/'config.json').read_text())
    board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text())
    m=manifest(root);used=m['usage']['review_calls']
    vlm=VisionClient(root,cfg['model_service_url'],max_calls=max(0,40-used),max_seconds=max(1,spec['deadline_epoch']-time.time()))
    artifacts=root/'comparison_review';artifacts.mkdir(exist_ok=True)
    path=root/'comparison_review.json';results=json.loads(path.read_text()) if path.exists() else []
    try:
        for cell in dict.fromkeys(['baseline','m1_low','m1_high','m2',spec.get('m2_final_cell','m2'),*spec.get('m2_final_shots',{}).values()]):
            numbers=[int(n) for n,c in spec.get('m2_final_shots',{}).items() if c==cell] if cell in spec.get('m2_final_shots',{}).values() else [0,*spec['shot_numbers']]
            for n in numbers:
                if any(r['cell']==cell and r['shot_number']==n for r in results):continue
                entry=next((e for e in board['shots'] if e['number']==n),None)
                image=root/portrait['image']['path'] if n==0 else root/next(r['image']['path'] for r in m['images'] if r['cell']==cell and r['shot']==f'shot{n:02d}')
                if n==0:
                    face=root/portrait['face']['path'];bbox={'status':'pass','source':'approved_portrait_bbox'}
                    from storycg.reference import default_outfit
                    outfit=default_outfit(identity)
                else:
                    outfit=entry['outfit']
                    bbox=vlm.call(f'{cell}_{n}_bbox',messages('Locate main protagonist face, including forehead, eyes, mouth, chin and ears. Return normalized [left,top,right,bottom]. No face => found=false.',[image]),BBOX_SCHEMA,[image])
                    face=artifacts/f'{cell}_{n}_face.png'
                    if bbox['status']=='pass' and bbox['data']['found']:
                        try:crop_face(image,bbox['data']['bbox'],face)
                        except ValueError:face=None
                    else:face=None
                checks=identity_checklist(identity,outfit)
                pair=artifacts/f'{cell}_{n}_pair.png'
                compare=[root/portrait['image']['path'],image]
                if face:
                    face_pair(root/portrait['face']['path'],face,pair);compare.insert(0,pair)
                text=('Audit every script-generated identity and outfit key independently. '
                      'First image if 3 inputs is face pair: LEFT approved portrait reference, RIGHT current. '
                      'Final two images are approved full portrait then current full image. '
                      'Compare sourced identity expectations AND visible reference separately in each reason. '
                      'Examine eye color and all visible accessory details carefully. '
                      'Do not infer a hidden required feature. Unobservable or ambiguous required feature => fail. '
                      'Empty expectation is unsourced: compare only reference and state it is unspecified. '
                      'pass=clear match; minor=small discrepancy; fail=major mismatch/unknown. Checklist: '+json.dumps(checks,ensure_ascii=False))
                result=vlm.call(f'{cell}_{n}_consistency',messages(text,compare),grading_schema(checks),compare)
                row={'cell':cell,'shot_number':n,'source_image':str(image.relative_to(root)),'source_sha256':digest(image),
                     'reference_image':portrait['image'],'checklist':checks,'bbox':bbox,
                     'face_pair':str(pair.relative_to(root)) if face else None,'audit':result,
                     'conclusion':conclude(result_statuses(result)+(['pass'] if face else ['fail']))}
                results.append(row);save_json_atomic(path,results);manifest(root)
                print(cell,n,row['conclusion'],flush=True)
    finally:
        vlm.close();manifest(root)

if __name__=='__main__':run(sys.argv[1])
