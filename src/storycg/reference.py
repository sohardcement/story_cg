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
    from storycg.review import (messages, grading_schema, BBOX_SCHEMA, crop_face,
                               identity_checklist, result_statuses, conclude, VISIBILITY_PROMPT,
                               enforce_visibility, annotate_reliability, load_reliability)
    root=Path(root)
    bbox=vlm.call(f'portrait_bbox_{attempt}',messages(
        'Locate the single protagonist face with ears, forehead and chin. Return normalized '
        '[left, top, right, bottom]; found=false if no identifiable face.',[image]),BBOX_SCHEMA,[image])
    face=None
    if bbox['status']=='pass' and bbox['data']['found']:
        try:
            face=crop_face(image,bbox['data']['bbox'],root/'portrait'/f'face_{attempt}.png')
            face['path']=str(Path(face['path']).relative_to(root))
        except ValueError:
            pass
    checks=identity_checklist(identity,default_outfit(identity))
    # Each outfit source tag remains explicit, including overlapping/layered components.
    for n,item in enumerate(identity['default_outfit_tags'],1):
        checks[f'outfit_component_{n}']={'expected':item,'source':'identity.json:default_outfit_tags'}
    checks.update({k:{'expected':v,'source':'config:portrait'} for k,v in {
        'body':'correct natural anatomy','hands':'correct visible hands',
        'composition':config['portrait']['tags']}.items()})
    paths=[image]+([root/face['path']] if face else [])
    audit=vlm.call(f'portrait_audit_{attempt}',messages(
        VISIBILITY_PROMPT + 'Audit this character setting portrait against ONLY the sourced checklist. '
        'Image 1 is full portrait; image 2 if present is its face crop. '
        'Assess every feature and clothing component, anatomy and hands with concrete visible evidence. '
        'pass=clear match; minor=small discrepancy; fail=major mismatch or unknown. '
        'Unobservable required features => not_visible. Empty expectations are unsourced: '
        'do not invent a canonical feature. Checklist: '+json.dumps(checks,ensure_ascii=False),paths),
        grading_schema(checks, visibility=True),paths)
    enforce_visibility(audit)
    annotate_reliability(audit, load_reliability(config))
    statuses=result_statuses(audit)+(['pass'] if face else ['fail'])
    return {'status':'pass' if conclude(statuses)=='通过' else 'fail',
            'conclusion':conclude(statuses),'bbox':bbox,'audit':audit,'checklist':checks,'face':face}


def ensure_portrait(job_dir, identity, config, client, *, cache_dir='runs/portrait_cache', vlm=None):
    import copy
    from storycg.runner import run_gen_one
    from storycg.review import VisionClient
    root=Path(job_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    key=cache_key(identity,config)
    if (root/'portrait.json').exists():
        record=require_portrait(root)
        if record['cache_key']!=key: raise ValueError('portrait identity/style cache mismatch')
        return record
    cached=reuse_portrait(root,cache_dir,key)
    if cached: return cached
    owned=vlm is None
    vlm=vlm or VisionClient(root,config['model_service_url'],max_calls=6)
    cfg=copy.deepcopy(config);cfg.pop('generation_reference',None)
    cfg['auto_retry_failed']=False
    cfg['prefix_namespace']=root.name+'_portrait'
    cfg['budget']['max_images_per_job']=min(3,config['budget']['max_images_per_job'])
    attempts=[];additions=[]
    try:
        for number in range(1,min(3,config['portrait']['max_attempts'],cfg['budget']['max_images_per_job'])+1):
            state=run_gen_one(root/'portrait',f'portrait{number}',portrait_prompt(identity,cfg,additions),cfg['portrait']['framing'],cfg,client)
            a=state['attempts'][-1]
            if a['status']!='completed': raise ValueError('portrait generation did not complete')
            image=root/'portrait'/a['image']['path']
            result=audit_portrait(root,image,identity,cfg,vlm,number)
            attempts.append({'attempt':number,'image':a['image'],'prompt_id':a['prompt_id'],'additions':list(additions),**result})
            save_json_atomic(root/'portrait_attempts.json',attempts)
            if result['status']=='pass':
                record={'status':'pass','cache_key':key,'identity_hash':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
                        'source_task':str(root),'cache_hit':False,'image':{**a['image'],'path':str(image.relative_to(root))},
                        'face':result['face'],'audit_file':'portrait_attempts.json','approved_attempt':number}
                save_json_atomic(root/'portrait.json',record)
                store_portrait(root,cache_dir)
                return require_portrait(root)
            failed=[k for k,v in result['audit'].get('data',{}).items() if v['status']!='pass']
            changes=[]
            for k in failed:
                changes.extend(config['portrait']['repair_tags'].get(k,config['portrait']['repair_tags']['identity']))
                if k.startswith('outfit_component_'):
                    item=identity['default_outfit_tags'][int(k.rsplit('_',1)[1])-1]
                    changes.append('('+item['tag'].replace('_',' ')+':1.2)')
                elif k in ['eye_color','hair_color','hairstyle','signature_features','accessories','hair_ornaments']:
                    changes.extend('('+item['tag'].replace('_',' ')+':1.2)' for item in result['checklist'][k]['expected'])
            # Preserve original identity; repairs reinforce only sourced terms and generic composition.
            changes=[v for v in dict.fromkeys(changes) if v not in additions]
            attempts[-1]['diagnostic_repair']={'diagnosis':result['audit'],
                'hypothesis':'reinforce failed sourced features and simplify pose visibility',
                'changes':changes,'result':'pending next attempt' if changes and number<3 else 'stopped'}
            save_json_atomic(root/'portrait_attempts.json',attempts)
            if not changes: break
            additions.extend(changes)
        save_json_atomic(root/'portrait.json',{'status':'fail','cache_key':key,'source_task':str(root),'attempts':len(attempts),
                                               'reason':'portrait did not pass every required check'})
        raise ValueError('portrait audit failed; story generation stopped')
    finally:
        if owned: vlm.close()


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
