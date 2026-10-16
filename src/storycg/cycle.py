"""Candidate audit and final review. Frozen review functions are reused verbatim.

V2-28: the repair loop (diagnosis, repair schedule, improvement rules) was removed with the repair
delivery; candidate delivery uses audit_candidate, full_review, failure_tier and the helpers below.
deliver moved to storycg.delivery.
"""
import json
from pathlib import Path
from storycg import review
from storycg.state import save_json_atomic
from storycg.reference import digest
from storycg.budget import CycleClient, count_usage

CONFIG_PATH=Path(__file__).resolve().parents[2]/'config/cycle.json'
# D29 failure tiers, used by the candidate ranking (candidates.rank_key).
REPAIR_IDENTITY_ITEMS=frozenset(('character','hair_color','hairstyle','eye_color',
    'signature_features','hair_ornaments','accessories','eye_color_sampled',
    'accessory_color_sampled','accessories_zoom','hair_ornaments_zoom'))


def settings():return json.loads(CONFIG_PATH.read_text())


def failure_tier(key):
    if key in ('protagonist_count','extra_body_part'):return 0  # V2-16: same level as the count gate (D29)
    if key in REPAIR_IDENTITY_ITEMS:return 1
    if review.reliability_key(key)=='outfit' or key=='outfit_detail_vs_portrait':return 2
    return 3


def read(path):return json.loads(Path(path).read_text())


def usage(root):return count_usage(root)


def selected_attempt(state,number,selected=None):
    matches=[a for a in state['attempts'] if a['shot_id']==f'shot{number:02d}' and a['status']=='completed']
    return next((a for a in matches if a['attempt_number']==selected),matches[-1])


def issues(results):
    items=[]
    for r in results:
        if r.get('status')!='pass':items.append({'key':'audit_error','status':'fail','reason':r.get('error','unknown'),'reliability':'unverified'})
        for key,item in r.get('data',{}).items():
            if isinstance(item,dict) and item.get('status') in ('fail','minor','not_visible'):
                items.append({'key':key,**item})
    return items



def audit_candidate(root,number,image,entry,board,identity,config,vlm,label):
    """V2-61 (D96, TASK-v2 §12 item 3): the screen of one candidate before the pick, no longer a full audit.

    Local detection (protagonist count, accessory colour sample) and, unless the count already reports a duplicate,
    the one extra-body-part question. There is no single-shot or identity/outfit review here: the selected
    candidate's identity and outfit are reviewed once, in the final review. A stored record is reused as is,
    including the older full audits (with single and consistency) of jobs made before V2-61.
    """
    path=root/'cycle_artifacts'/f'{label}.json'
    if path.exists():
        result=read(path)
        if result['image']['sha256']!=image['sha256']:raise ValueError('audit image mismatch')
        return result
    source=root/image['path']
    if digest(source)!=image['sha256']:raise ValueError('candidate hash mismatch')
    from storycg.detection_review import detect_face_record, audit_detection
    from storycg import extra_part
    face_path=root/'cycle_artifacts'/f'{label}_face.png'
    detected,face=detect_face_record(source,face_path)
    if face:face['path']=str(face_path.relative_to(root))
    portrait=read(root/'portrait.json')
    detection_items=audit_detection(source,identity,entry,reference_path=root/portrait['image']['path'],review_config=config)
    extra=(extra_part.check(vlm,source,identity,label+'_extra_body_part')
           if extra_part.should_check(detection_items) and config.get('review_mode')!='human' else None)  # D101: human review makes no vision call  # V2-16: skipped on a reliable duplicate count
    result={'version':2,'image':image,'detection':detected,'face':face,'detection_items':detection_items,
            'extra_body_part':extra}
    save_json_atomic(path,result);return result


def full_review(root,config,budget):
    calls=budget.settings['max_vision_calls']-budget.usage()['review_calls']
    owned=CycleClient(root,config['model_service_url'],budget=budget,max_calls=max(0,calls),max_seconds=None)
    # P0.2 (D97): the per-shot identity/outfit review is off in delivery unless the config turns it on.
    per_shot=(config.get('final_review') or {}).get('per_shot_consistency',False)
    try:return review.review_job(root,config,client=owned,per_shot_consistency=per_shot)
    finally:owned.close()
