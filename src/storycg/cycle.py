"""Candidate audit and final review. Frozen review functions are reused verbatim.

V2-28: the repair loop (diagnosis, repair schedule, improvement rules) was removed with the repair
delivery; candidate delivery uses audit_candidate, full_review, failure_tier and the helpers below.
deliver moved to storycg.delivery.
"""
import json
from pathlib import Path
from storycg import review
from storycg.closed_eyes import drop_eye_colour_check, mark_eye_colour_not_applicable
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


def consistency_review(vlm,paths,identity,entry,config,purpose):
    """Identity/outfit consistency review. V2-14 (D58): a closed-eye shot has no eye colour to judge."""
    checks=review.identity_checklist(identity,entry['outfit'])
    eyes_not_applicable=drop_eye_colour_check(checks,entry['shot'],config)
    consistency=review.audit_consistency(vlm,paths,checks,config,purpose)
    if eyes_not_applicable:mark_eye_colour_not_applicable(consistency)
    return consistency


def audit_candidate(root,number,image,entry,board,identity,config,vlm,label):
    path=root/'cycle_artifacts'/f'{label}.json'
    if path.exists():
        result=read(path)
        if result['image']['sha256']!=image['sha256']:raise ValueError('audit image mismatch')
        return result
    source=root/image['path']
    if digest(source)!=image['sha256']:raise ValueError('candidate hash mismatch')
    single=review.audit_single(vlm,source,entry,board['props'],config,label+'_single')
    from storycg.detection_review import detect_face_record, audit_detection
    face_path=root/'cycle_artifacts'/f'{label}_face.png'
    detected,face=detect_face_record(source,face_path)
    portrait=read(root/'portrait.json');paths=[root/portrait['image']['path'],source]
    if face:
        face['path']=str(face_path.relative_to(root))
        pair=root/'cycle_artifacts'/f'{label}_pair.png'
        # The reference pair also uses a local crop, including legacy portraits.
        ref_path=root/'cycle_artifacts'/f'{label}_reference_face.png'
        _,ref_face=detect_face_record(paths[0],ref_path)
        if ref_face:
            review.face_pair(ref_path,face_path,pair);paths.append(pair)
    detection_items=audit_detection(source,identity,entry,reference_path=paths[0], review_config=config)
    consistency=consistency_review(vlm,paths,identity,entry,config,label+'_identity_outfit')
    from storycg import extra_part
    if extra_part.should_check(detection_items):  # V2-16: one vision question, skipped on a reliable duplicate count
        consistency=extra_part.merge_into(consistency,extra_part.check(vlm,source,identity,label+'_extra_body_part'))
    result={'image':image,'single':single,'consistency':consistency,'detection':detected,'face':face,'detection_items':detection_items,
            'conclusion':review.conclude([*review.result_statuses(single),*review.result_statuses(consistency),*review.result_statuses(detection_items['review'])])}
    save_json_atomic(path,result);return result


def full_review(root,config,budget):
    calls=budget.settings['max_vision_calls']-budget.usage()['review_calls']
    owned=CycleClient(root,config['model_service_url'],budget=budget,max_calls=max(0,calls),max_seconds=budget.remaining_seconds())
    try:return review.review_job(root,config,client=owned)
    finally:owned.close()
