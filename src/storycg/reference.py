"""Approved portrait gate and content-addressed reuse; no character-specific rules."""
import hashlib
import json
import shutil
from pathlib import Path
from storycg.state import save_json_atomic


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cache_key(identity, config):
    content={'identity':identity,'style':{k:config[k] for k in ['checkpoint','style_loras','sampler','resolutions','positive_prefix','negative_prompt']},'portrait':config.get('portrait',{})}
    return hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def require_portrait(job_dir):
    root=Path(job_dir)
    try:
        record=json.loads((root/'portrait.json').read_text())
        if record['status']!='pass': raise ValueError('not approved')
        for key in ['image','face']:
            path=(root/record[key]['path']).resolve()
            if not path.is_relative_to(root.resolve()) or digest(path)!=record[key]['sha256']:
                raise ValueError('reference hash/path mismatch')
        return record
    except (KeyError,OSError,ValueError) as exc:
        raise ValueError(f'approved portrait required: {exc}') from exc


def reuse_portrait(job_dir, cache_dir, key):
    source=Path(cache_dir)/key
    try:
        record=require_portrait(source)
    except ValueError:
        return None
    if record.get('cache_key')!=key: return None
    root=Path(job_dir);root.mkdir(parents=True,exist_ok=True)
    for item in ['image','face']:
        target=root/'portrait'/Path(record[item]['path']).name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/record[item]['path'],target)
        record[item]['path']=str(target.relative_to(root))
    record['source_audit']={'source_task':record['source_task'],'path':record.get('audit_file')}
    record['cache_hit']=True
    save_json_atomic(root/'portrait.json',record)
    return require_portrait(root)


def store_portrait(job_dir, cache_dir):
    root=Path(job_dir);record=require_portrait(root)
    dest=Path(cache_dir)/record['cache_key']
    if dest.exists(): return
    dest.mkdir(parents=True)
    for item in ['image','face']:
        target=dest/Path(record[item]['path']).name
        shutil.copyfile(root/record[item]['path'],target)
        record[item]['path']=target.name
    save_json_atomic(dest/'portrait.json',record)


def default_outfit(identity):
    tags=[item['tag'] for item in identity['default_outfit_tags']]
    return {'tags':tags,'clothing_state':', '.join(tags),'change_at_shot':None}


def portrait_prompt(identity, config, additions=()):
    return ', '.join([identity['identity_block'].replace('_',' '),
                      *(t['tag'].replace('_',' ') for t in identity['default_outfit_tags']),
                      *config['portrait']['tags'], *additions])


def audit_portrait(root, image, identity, config, vlm, attempt):
    from storycg.review import (messages, grading_schema,
                               identity_checklist, result_statuses, conclude, OBSERVABILITY_PROMPT,
                               enforce_visibility, annotate_reliability, load_reliability)
    root=Path(root)
    from storycg.detection_review import detect_face_record, audit_detection
    bbox,face=detect_face_record(image,root/'portrait'/f'face_{attempt}.png')
    if face:
        face['path']=str(Path(face['path']).relative_to(root))
    detection_items=audit_detection(image,identity,{'shot':{}}, review_config=config)
    checks=identity_checklist(identity,default_outfit(identity))
    # Each outfit source tag remains explicit, including overlapping/layered components.
    for n,item in enumerate(identity['default_outfit_tags'],1):
        checks[f'outfit_component_{n}']={'expected':item,'source':'identity.json:default_outfit_tags'}
    checks.update({k:{'expected':v,'source':'config:portrait'} for k,v in {
        'body':'correct natural anatomy','hands':'correct visible hands',
        'composition':config['portrait']['tags']}.items()})
    paths=[image]+([root/face['path']] if face else [])
    if config.get('review_mode') == 'human':  # D101: the user looks at the portrait; no vision-model call
        return {'status':'pass' if face else 'fail','conclusion':'未审核（用户看图）','bbox':bbox,'audit':{'status':'skipped','data':{}},
                'checklist':checks,'face':face,'detection_items':detection_items}
    audit=vlm.call(f'portrait_audit_{attempt}',messages(
        OBSERVABILITY_PROMPT + 'Audit this character setting portrait against ONLY the sourced checklist. '
        'Image 1 is full portrait; image 2 if present is its face crop. '
        'Assess every feature and clothing component, anatomy and hands with concrete visible evidence. '
        'pass=clear match; minor=small discrepancy; fail=major observable mismatch or absence. '
        'Unobservable required features => not_visible. Empty expectations are unsourced: '
        'do not invent a canonical feature. Checklist: '+json.dumps(checks,ensure_ascii=False),paths),
        grading_schema(checks, visibility=True, observability=True),paths)
    enforce_visibility(audit)
    annotate_reliability(audit, load_reliability(config))
    statuses=result_statuses(audit)+result_statuses(detection_items['review'])+(['pass'] if face else ['fail'])
    return {'status':'pass' if conclude(statuses)=='通过' else 'fail',
            'conclusion':conclude(statuses),'bbox':bbox,'audit':audit,'checklist':checks,'face':face,
            'detection_items':detection_items}


def _finish_single(root, key, identity, record, cache_dir):
    """V2-21 (TASK-v2 §10.3 a): the portrait is used as is; the audit result is recorded, never a veto.

    `record` is one attempt entry as ensure_portrait writes it: {'attempt', 'image' (the ComfyUI image,
    path relative to portrait/), 'status', 'conclusion', 'face' (path relative to the job), ...}.
    """
    if not record.get('face'):raise ValueError('portrait has no localized face; IPAdapter reference needs it')
    save_json_atomic(root/'portrait.json',{'status':'pass','gate_policy':'single_no_gate','raw_status':record['status'],
        'raw_conclusion':record['conclusion'],'gate_notes':['定妆图只出 1 张，审核结论只记录，不否决、不修复（TASK-v2 §10.3 a）'],
        'cache_key':key,'identity_hash':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
        'source_task':str(root),'cache_hit':False,
        'image':{**record['image'],'path':'portrait/'+record['image']['path']},
        'face':record['face'],'audit_file':'portrait_attempts.json','approved_attempt':record['attempt']})
    store_portrait(root,cache_dir)
    return require_portrait(root)


def portrait_redo_seed(config, job_id, number):
    """V2-24: seed of a redone portrait (attempt `number`); a function of the job's base seed and the attempt."""
    base = config.get('generation_seed')
    if base is None:
        base = int(hashlib.sha256(job_id.encode()).hexdigest()[:15], 16)
    digest = hashlib.sha256(f'{int(base)}/portrait/{number}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)


# V2-28: the portrait stage allows 3 attempts (the first one and at most 2 redos). The value used to be
# config/repairs.json portrait.max_repairs (2) + 1; that file was removed with the repair delivery.
PORTRAIT_MAX_ATTEMPTS = 3


def ensure_portrait(job_dir, identity, config, client, *, cache_dir='runs/portrait_cache', vlm=None, redo=False):
    """One portrait, used as is: its audit is recorded, never a veto (TASK-v2 §10.3 a).

    `redo` (V2-24): generate one more portrait with a new deterministic seed and make it the approved one.
    It skips the reuse cache and the completed-portrait shortcut."""
    import copy
    from storycg.runner import run_gen_one
    from storycg.review import VisionClient
    root=Path(job_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    key=cache_key(identity,config)
    prior={}
    if (root/'portrait.json').exists():
        prior=json.loads((root/'portrait.json').read_text())
        if prior.get('cache_key')!=key:raise ValueError('portrait identity/style cache mismatch')
        if prior.get('status')=='pass' and not redo:return require_portrait(root)
    cached=None if redo else reuse_portrait(root,cache_dir,key)
    if cached:return cached
    owned=vlm is None
    vlm=vlm or VisionClient(root,config['model_service_url'],max_calls=2*PORTRAIT_MAX_ATTEMPTS)
    cfg=copy.deepcopy(config);cfg.pop('generation_reference',None)
    cfg['auto_retry_failed']=False
    cfg['prefix_namespace']=root.name+'_portrait'
    # This is a local stage cap; the job-wide budget remains unchanged.
    cfg['budget']['max_images_per_job']=min(PORTRAIT_MAX_ATTEMPTS,config['budget']['max_images_per_job'])
    path=root/'portrait_attempts.json'
    attempts=json.loads(path.read_text()) if path.exists() else []
    if redo and not attempts:raise ValueError('no portrait attempt to redo')
    try:
        if redo:
            # V2-24: a fresh attempt with its own seed; the audit is recorded, never a veto.
            number=len(attempts)+1
            state=run_gen_one(root/'portrait',f'portrait{number}',portrait_prompt(identity,cfg),cfg['portrait']['framing'],cfg,client,
                              seed=portrait_redo_seed(config,root.name,number))
        elif attempts:
            # V2-21 rework 2: the last completed attempt is adopted on every path (fresh or resumed);
            # no gate, no instruction edit, no new submission.
            return _finish_single(root,key,identity,attempts[-1],cache_dir)
        else:
            number=1
            state=run_gen_one(root/'portrait','portrait1',portrait_prompt(identity,cfg),cfg['portrait']['framing'],cfg,client)
        a=state['attempts'][-1]
        if a['status']!='completed':raise ValueError('portrait generation did not complete')
        result=audit_portrait(root,root/'portrait'/a['image']['path'],identity,cfg,vlm,number)
        record={'attempt':number,'image':a['image'],'prompt_id':a['prompt_id'],**result}
        attempts.append(record);save_json_atomic(path,attempts)
        return _finish_single(root,key,identity,record,cache_dir)
    finally:
        if owned:vlm.close()
