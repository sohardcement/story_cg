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


def reuse_l3_portrait(job_dir, identity, config, *, search_roots=None):
    """Reuse project-local approved L3 records without importing generation usage."""
    import copy
    from storycg.fallback import current_l3_approval
    project=Path(__file__).resolve().parents[2]
    search_roots=search_roots if search_roots is not None else [project/'runs',project/'jobs']
    root=Path(job_dir).resolve()
    fields=('character_tag','work_tag','identity_block','outfit_block','appearance_tags',
            'default_outfit_tags','default_outfit_selection')
    for base in search_roots:
        for path in sorted(Path(base).rglob('portrait.json')):
            source=path.parent.resolve()
            if source==root:continue
            try:
                record=json.loads(path.read_text());approved=record.get('approved_identity')
                if (record.get('gate_policy')!='L3 reliable comparison' or not approved or
                    approved.get('identity_tier')!='L3' or
                    approved.get('tier_model_fingerprint')!=identity.get('tier_model_fingerprint') or
                    any(approved.get(k)!=identity.get(k) for k in fields) or
                    record.get('cache_key')!=cache_key(approved,config) or
                    not current_l3_approval(approved)):continue
                record=require_portrait(source)
            except (ValueError,KeyError,OSError):continue
            approved=copy.deepcopy(approved)
            approved['tier_evidence']['aggregation_policy']='D8b'
            approved['tier_evidence']['portrait_cache_source']={'task':str(source),
                'portrait_sha256':digest(path),'audit_file':record.get('audit_file')}
            for key in ('image','face'):
                original=(source/record[key]['path']).resolve()
                target=root/'portrait'/(key+original.suffix)
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,target)
                record[key]['path']=str(target.relative_to(root))
            record.update(cache_hit=True,source_audit={'source_task':str(source),'path':record.get('audit_file')},
                          approved_identity=approved,cache_key=cache_key(approved,config))
            save_json_atomic(root/'portrait.json',record)
            save_json_atomic(root/'identity.json',approved)
            return approved
    return None


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


def portrait_repair_instruction(identity, result):
    """D6 attribute edits use only exact locked identity/outfit tags."""
    from storycg.editing import grounded_instruction
    state={'identity':identity,'entry':{},'props':[]}
    instructions=[];edits=[]
    for key,item in result.get('audit',{}).get('data',{}).items():
        if item.get('status')!='fail':continue
        if key not in ('eye_color','hair_color','hairstyle','signature_features','accessories','hair_ornaments','outfit','outfit_state') and not key.startswith('outfit_component_'):continue
        expected=result.get('checklist',{}).get(key,{}).get('expected',[])
        if key in ('outfit','outfit_state'):expected=identity['default_outfit_tags']
        if isinstance(expected,dict):expected=[expected]
        allowed={v['tag'] for v in expected if isinstance(v,dict) and 'tag' in v}
        for group in ('appearance_tags','default_outfit_tags','locked_tags'):
            for n,tag in enumerate(identity.get(group,[])):
                if tag['tag'] not in allowed:continue
                edit={'operation':'set','target':f'identity.{group}.{n}.tag','description':tag['tag']}
                if edit in edits:continue
                edits.append(edit);instructions.append(grounded_instruction(edit,state))
    return ' '.join(instructions),edits


def ensure_portrait(job_dir, identity, config, client, *, cache_dir='runs/portrait_cache', vlm=None):
    import copy
    from storycg.runner import run_gen_one
    from storycg.review import VisionClient
    from storycg.portrait_gate import gate_accepts
    from storycg.controls import upload_reference
    root=Path(job_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    key=cache_key(identity,config)
    prior={}
    if (root/'portrait.json').exists():
        prior=json.loads((root/'portrait.json').read_text())
        if prior.get('cache_key')!=key:raise ValueError('portrait identity/style cache mismatch')
        if prior.get('status')=='pass':return require_portrait(root)
    cached=reuse_portrait(root,cache_dir,key)
    if cached:return cached
    repair_settings=json.loads((Path(__file__).resolve().parents[2]/'config/repairs.json').read_text())
    limit=repair_settings['portrait']['max_repairs']
    owned=vlm is None
    vlm=vlm or VisionClient(root,config['model_service_url'],max_calls=2*(limit+1))
    cfg=copy.deepcopy(config);cfg.pop('generation_reference',None)
    cfg['auto_retry_failed']=False
    cfg['prefix_namespace']=root.name+'_portrait'
    # This is a local stage cap; the job-wide budget remains unchanged.
    cfg['budget']['max_images_per_job']=min(limit+1,config['budget']['max_images_per_job'])
    path=root/'portrait_attempts.json'
    attempts=json.loads(path.read_text()) if path.exists() else []
    try:
        while True:
            if attempts:
                result=attempts[-1]
                accepted,notes=gate_accepts(result)
                if accepted:
                    record={'status':'pass','gate_policy':'D7','raw_status':result['status'],
                        'raw_conclusion':result['conclusion'],'gate_notes':notes,'cache_key':key,
                        'identity_hash':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
                        'source_task':str(root),'cache_hit':False,
                        'image':{**result['image'],'path':'portrait/'+result['image']['path']},
                        'face':result['face'],'audit_file':'portrait_attempts.json','approved_attempt':result['attempt']}
                    save_json_atomic(root/'portrait.json',record);store_portrait(root,cache_dir)
                    return require_portrait(root)
                repair_count=sum('diagnostic_repair' in a and a['diagnostic_repair'].get('primitive')=='instruction_edit' for a in attempts)
                if repair_count>=limit:break
                instruction,edits=portrait_repair_instruction(identity,result)
                if not instruction:break
                diagnosis={'primitive':'instruction_edit','diagnosis':result['audit'],
                    'hypothesis':'Correct the failed sourced attributes with instruction editing while preserving other features.',
                    'changes':edits,'instruction':instruction,'source_attempt':result['attempt'],
                    'mandatory_reaudit':'complete portrait audit','result':'pending'}
                number=len(attempts)+1
                # Persist the request before upload/submission; recovery reuses its shot ID.
                save_json_atomic(root/'portrait_repair_request.json',diagnosis)
                image=root/'portrait'/result['image']['path']
                if digest(image)!=result['image']['sha256']:raise ValueError('portrait repair source hash mismatch')
                ref={'mechanism':'qwen_edit','strength':1.0,'uploaded_image':upload_reference(root,image,client),
                     'path':str(image.relative_to(root)),'sha256':result['image']['sha256']}
                state=run_gen_one(root/'portrait',f'portraitedit{repair_count+1}',instruction,cfg['portrait']['framing'],cfg,client,
                                  generation_reference=ref,repair_diagnosis={k:diagnosis[k] for k in ('diagnosis','hypothesis','changes')})
            else:
                number=1;diagnosis=None
                state=run_gen_one(root/'portrait','portrait1',portrait_prompt(identity,cfg),cfg['portrait']['framing'],cfg,client)
            a=state['attempts'][-1]
            if a['status']!='completed':raise ValueError('portrait generation did not complete')
            result=audit_portrait(root,root/'portrait'/a['image']['path'],identity,cfg,vlm,number)
            record={'attempt':number,'image':a['image'],'prompt_id':a['prompt_id'],**result}
            if diagnosis:
                diagnosis['result']={'status':result['status'],'conclusion':result['conclusion'],'audit':result['audit']}
                record['diagnostic_repair']=diagnosis
            attempts.append(record);save_json_atomic(path,attempts)
        save_json_atomic(root/'portrait.json',{'status':'fail','cache_key':key,'source_task':str(root),'attempts':len(attempts),
            'reason':'portrait did not pass every required check; repair cap exhausted or no grounded repair target'})
        raise ValueError('portrait audit failed; story generation stopped')
    finally:
        if owned:vlm.close()


def edit_instruction(entry, props):
    """Short edit instruction, with action and framing before environment details."""
    shot=entry['shot'];scene=entry['scene'];registry={p['id']:p for p in props}
    bindings=[]
    for ref in shot['key_props']:
        description=', '.join(t.replace('_',' ') for t in registry[ref['prop_id']]['description_tags'])
        bindings.append(f"Object: {description}. Holder: {ref['held_by']}. Position: {ref['position']}.")
    return ('Edit the reference into one new scene. Change the pose and camera: '
            f"{shot['framing']} framing, {shot['camera_angle']} camera. Action: {shot['action']}. "
            f"Expression: {shot['expression']}. Spatial arrangement: {shot['spatial_relation']}. "
            + ' '.join(bindings) + ' Preserve the same person, face, eye color, hairstyle, ears, '
            'accessories and complete outfit as the reference. One protagonist only. '
            + 'Environment: '+', '.join(str(scene.get(k,'')) for k in ['location','time','weather','lighting','environment_change'])
            + '. Outfit: '+entry['outfit']['clothing_state']+'. No captions or panel borders.')
