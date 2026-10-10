"""Runtime aggregation outside the v2 freeze. Never rewrites reviewer grades."""
import hashlib
from pathlib import Path
from storycg.review import load_reliability, result_statuses, conclude
from storycg.state import save_json_atomic

from storycg.v1_scope import person_findings, narrative_findings, is_prop_item, scope_context

DETECTION_KEYS=('accessory_color_sampled','protagonist_count')


def detection_gate(observations, config=None):
    reliability=load_reliability(config or {})
    review=observations.get('review',{})
    if not observations:return [],[]  # Legacy audits did not run the detector.
    if review.get('status')!='pass':return ['fail'],['检测调用失败：'+review.get('error','unknown')]
    statuses=[];notes=[]
    keys=(*DETECTION_KEYS, *(('eye_color_sampled',) if 'eye_color_sampled' in review.get('data',{}) else ()))
    for key in keys:
        item=review.get('data',{}).get(key,{})
        status=item.get('status');reliable=reliability.get(key,{}).get('status')=='reliable'
        if status=='not_visible' or item.get('observation',{}).get('status',item.get('observed_status')) in ('not_visible','none_visible'):
            notes.append(key+': 该项未经审核（检测弃权）')
        elif not reliable:
            notes.append(key+': '+str(status)+'；只记录，不否决（该项未经可靠审核）')
        elif status in ('fail','duplicate') or item.get('observation',{}).get('status',item.get('observed_status'))=='duplicate':
            statuses.append('fail')
            notes.append(key+': '+str(status)+'；可靠检测项否决')
        elif status=='pass':statuses.append('pass')
        else:
            statuses.append('fail');notes.append(key+': 可靠检测项缺失或未知')
    return statuses,notes


def group_gate(report, config=None, *, context=None):
    statuses=[];notes=[];known=[]
    # All visual rules stay exactly as the frozen reviewer, including not_visible.
    for layer in ('single','consistency'):
        rows=report.get(layer,[])
        if not rows:statuses.append('fail')
        for row in rows:
            statuses.extend(result_statuses(person_findings(row)))
            for key,item in row.get('data',{}).items():
                if is_prop_item(key) and item.get('status')!='pass':
                    known.append(f"{layer} shot {row.get('shot')}: {key}: {item.get('status')} — {item.get('reason','')}；道具项只记录，不否决、不修复")
    statuses.extend(row.get('status','fail') for row in report.get('faces',[]))
    statuses.append(report.get('narrative_blind',{}).get('status','fail'))
    statuses.extend(result_statuses(narrative_findings(report.get('narrative_compare',{}),context)))
    for row in report.get('detection_items',[]):
        grades,observed_notes=detection_gate(row,config)
        statuses.extend(grades);notes.extend(f"shot {row['shot']}: {n}" for n in observed_notes)
    if not report.get('detection_items'):statuses.append('fail')
    if (context or {}).get('prop_dependent') is True:
        for key in (context or {}).get('prop_only_narrative_items',[]):
            item=report.get('narrative_compare',{}).get('data',{}).get(key,{})
            known.append(f"盲审 {key}: {item.get('status','unknown')} — {item.get('reason','')}；接收阶段明确此项只依赖道具，只记录、不否决、不修复")
        notes.append('道具依赖剧情：混合人物/道具叙事项仍保守保留；不触发混合叙事修复。')
    return {'policy':'D23/D24 v1 person scope','conclusion':conclude(statuses),
        'raw_conclusion':report.get('conclusion'), 'notes':notes, 'known_problems':known}


def persist_group_gate(root, report, config=None):
    root=Path(root);gate=group_gate(report,config,context=scope_context(root))
    gate['raw_review_file']='review.json'
    if (root/'review.json').exists():gate['raw_review_sha256']=hashlib.sha256((root/'review.json').read_bytes()).hexdigest()
    save_json_atomic(root/'review_gate.json',gate)
    return gate
