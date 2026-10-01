"""D7 gate aggregation; immutable reviewer outputs remain in portrait_attempts.json."""
import hashlib
import json
from pathlib import Path
from storycg.reference import cache_key, require_portrait, store_portrait
from storycg.state import save_json_atomic


def gate_accepts(result):
    audit=result.get('audit',{})
    if audit.get('status')!='pass' or not result.get('face'):return False,[]
    checks=result.get('checklist',{});data=audit.get('data',{});notes=[]
    if not data or not checks:return False,[]
    for key,check in checks.items():
        item=data.get(key,{})
        if item.get('status')=='pass':continue
        if not check.get('expected') and not check.get('required') and item.get('status')=='not_visible':
            notes.append(key+': 该项未经审核（无来源期望且不可见）')
        else:return False,[]
    if any(key not in checks and item.get('status')!='pass' for key,item in data.items()):return False,[]
    from storycg.review_gate import detection_gate
    statuses,detection_notes=detection_gate(result.get('detection_items',{}))
    if 'fail' in statuses:return False,detection_notes
    return True,notes+detection_notes


def recover_d7_portrait(root,identity,config,cache_dir='runs/portrait_cache'):
    root=Path(root).resolve();path=root/'portrait.json'
    if not path.exists() or json.loads(path.read_text()).get('status')=='pass':return None
    attempt_path=root/'portrait_attempts.json'
    if not attempt_path.exists():return None
    attempts=json.loads(attempt_path.read_text())
    for result in reversed(attempts):
        accepted,notes=gate_accepts(result)
        if not accepted:continue
        original=json.loads(path.read_text())
        if original['cache_key']!=cache_key(identity,config):raise ValueError('D7 reference cache mismatch')
        record={'status':'pass','gate_policy':'D7','raw_status':result['status'],'raw_conclusion':result['conclusion'],
            'gate_notes':notes,'cache_key':original['cache_key'],
            'identity_hash':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
            'source_task':str(root),'cache_hit':False,'image':{**result['image'],'path':'portrait/'+result['image']['path']},
            'face':result['face'],'audit_file':'portrait_attempts.json','approved_attempt':result['attempt']}
        save_json_atomic(root/'portrait_gate_original.json',original)
        save_json_atomic(path,record)
        approved=require_portrait(root);store_portrait(root,cache_dir);return approved
    return None
