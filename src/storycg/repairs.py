"""Diagnosed, restartable repair primitives; no automatic repair selection."""
import copy
import json
import math
import shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter
from storycg.assembly import assemble_prompt
from storycg.controls import portrait_reference, upload_reference
from storycg.reference import digest, edit_instruction
from storycg.runner import _job_lock, _new_attempt, _process, _save, _now
from storycg.state import load_state, save_json_atomic
from storycg.planning import validate_storyboard


def validate_diagnosis(record):
    if not isinstance(record, dict) or any(not record.get(k) for k in ('diagnosis','hypothesis','changes','result')):
        raise ValueError('repair requires diagnosis, hypothesis, changes and result (pending until audited)')
    for key in ('diagnosis','hypothesis','changes'):
        if not isinstance(record[key],str) or not record[key].strip():
            raise ValueError('diagnostic sections must be nonempty text')
    if not isinstance(record['result'],dict) or record['result'].get('status') not in ('pending','reviewed'):
        raise ValueError('result must explicitly be pending or reviewed')


def region_pixels(bbox, size, expansion=0):
    if len(bbox)!=4 or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=1 for v in bbox) or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]:
        raise ValueError('invalid normalized region')
    if not 0<=expansion<=2: raise ValueError('invalid expansion')
    w,h=size;dx=(bbox[2]-bbox[0])*expansion;dy=(bbox[3]-bbox[1])*expansion
    return [max(0,math.floor((bbox[0]-dx)*w)),max(0,math.floor((bbox[1]-dy)*h)),min(w,math.ceil((bbox[2]+dx)*w)),min(h,math.ceil((bbox[3]+dy)*h))]


def feather_mask(size, pixels, feather):
    if feather<0: raise ValueError('negative feather')
    mask=Image.new('L',size,0); x0,y0,x1,y1=pixels
    if not (0<=x0<x1<=size[0] and 0<=y0<y1<=size[1]): raise ValueError('invalid pixel region')
    ImageDraw.Draw(mask).rectangle((x0,y0,x1-1,y1-1),fill=255)
    # Confine feathering to the specified region, preserving all outside pixels.
    blurred=mask.filter(ImageFilter.GaussianBlur(feather))
    from PIL import ImageChops
    return ImageChops.multiply(mask,blurred)


def composite_region(base, patch, pixels, feather):
    x0,y0,x1,y1=pixels
    layer=base.convert('RGB').copy();layer.paste(patch.convert('RGB').resize((x1-x0,y1-y0),Image.Resampling.LANCZOS),(x0,y0))
    return Image.composite(layer,base.convert('RGB'),feather_mask(base.size,pixels,feather))


def fork_repair_job(source, destination):
    source=Path(source).resolve(); destination=Path(destination).resolve()
    if destination==source or destination.is_relative_to(source): raise ValueError('repair destination must be independent')
    if (destination/'state.json').exists():
        state=load_state(destination)
        if state.get('repair_source')!=str(source): raise ValueError('repair source mismatch')
        return state
    if destination.exists() and any(destination.iterdir()): raise ValueError('destination must be empty')
    shutil.copytree(source,destination,ignore=shutil.ignore_patterns('.lock','review_log.jsonl','review_artifacts','cache'),dirs_exist_ok=True)
    save_json_atomic(destination/'source_manifest.json',json.loads((source/'manifest.json').read_text()))
    for name in ('review.json','review.md','contact_sheet.jpg'):
        if (destination/name).exists(): (destination/name).rename(destination/('source_'+name))
    state=load_state(destination); state['source_created_at']=state['created_at'];state['created_at']=_now()
    state['repair_source']=str(source);state['job_id']=destination.name
    state['config']['prefix_namespace']=destination.name;state['config']['auto_retry_failed']=False
    settings=json.loads((Path(__file__).resolve().parents[2]/'config/repairs.json').read_text())
    state['config']['repair_settings']=settings
    state['config']['budget']['max_images_per_job']=len(state['attempts'])+settings['max_new_images_per_repair_job']
    for a in state['attempts']: a['inherited']=True
    _save(destination,state)
    job=json.loads((destination/'job.json').read_text());job['job_id']=destination.name;save_json_atomic(destination/'job.json',job)
    # Do not present inherited reviews as reviews of repaired candidates.
    manifest=json.loads((destination/'manifest.json').read_text());manifest['job_id']=destination.name;manifest['review_records']=[];manifest['usage']['review_calls']=0
    save_json_atomic(destination/'manifest.json',manifest)
    return state


def repair(root, shot_number, primitive, diagnosis, client, *, bbox=None, region_source=None,
           prop_ids=None, denoise=None, seed=None, revised_entry=None, repair_id='repair1'):
    """Resume by repair_id; changing an existing request is rejected, never reposted."""
    validate_diagnosis(diagnosis)
    if primitive not in ('inpaint','face_detail','relayout','seed'): raise ValueError('unknown primitive')
    if primitive=='seed' and seed is None: raise ValueError('seed repair requires a seed')
    root=Path(root).resolve();shot_id=f'shot{shot_number:02d}'
    request={'primitive':primitive,'shot':shot_number,'diagnosis':diagnosis,'bbox':bbox,'region_source':region_source,'prop_ids':prop_ids,'denoise':denoise,'seed':seed,'revised_entry':revised_entry}
    with _job_lock(root):
        state=load_state(root);config=copy.deepcopy(state['config'])
        board=json.loads((root/'storyboard.json').read_text());identity=json.loads((root/'identity.json').read_text());portrait=json.loads((root/'portrait.json').read_text())
        if not 1 <= shot_number <= len(board['shots']):
            raise ValueError('shot number out of range')
        entry=copy.deepcopy(board['shots'][shot_number-1])
        if revised_entry:
            board['shots'][shot_number-1]=revised_entry
            validate_storyboard(board,len(board['shots']),identity)
            entry=revised_entry
        if primitive in ('inpaint','face_detail'):
            if not bbox or not region_source: raise ValueError('region and its provenance required')
            region_pixels(bbox,(1024,1024))
        if denoise is not None and not 0 < denoise <= 1:
            raise ValueError('denoise must be in (0,1]')
        records=state.setdefault('repairs',{}); key=f'{shot_id}_{repair_id}'
        if key in records:
            rec=records[key]
            if rec['request']!=request: raise ValueError('repair ID already has different inputs')
        else:
            source=next(a for a in reversed(state['attempts']) if a['shot_id']==shot_id and a['status']=='completed')
            rec={'request':request,'source_image':copy.deepcopy(source['image']),'source_attempt':source['attempt_number'],'diagnosis':copy.deepcopy(diagnosis),'stages':[], 'status':'prepared'}
            records[key]=rec;_save(root,state)
        settings=config.get('repair_settings') or json.loads((Path(__file__).resolve().parents[2]/'config/repairs.json').read_text())
        rec['settings']=settings
        parts=assemble_prompt(identity,entry,props=board['props'],config=config)
        face=portrait_reference(root,portrait,'face',client,'ipadapter',config['identity_control']['ipadapter_strength'])
        if seed is not None: config['generation_seed']=seed
        assets=root/'repair_assets'/key;assets.mkdir(parents=True,exist_ok=True)
        pixels=None;reference=face;tags=parts['positive_tags'];framing=parts['framing']
        if primitive in ('inpaint','face_detail'):
            if not bbox or not region_source: raise ValueError('region and its provenance required')
            source_path=root/rec['source_image']['path']
            if digest(source_path)!=rec['source_image']['sha256']: raise ValueError('source hash mismatch')
            with Image.open(source_path) as im:
                pixels=region_pixels(bbox,im.size,settings['face_detail']['bbox_expansion'] if primitive=='face_detail' else 0)
                crop=im.convert('RGB').crop(pixels)
                if not (assets/'input.png').exists():
                    crop.resize(tuple(settings[primitive]['resolution']),Image.Resampling.LANCZOS).save(assets/'input.png')
                if not (assets/'mask_full.png').exists():
                    feather_mask(im.size,pixels,settings[primitive]['feather_pixels']).save(assets/'mask_full.png')
            if primitive=='face_detail':
                tags=', '.join([parts['identity_block'],parts['outfit_block'],'face, head, portrait, '+entry['shot']['expression']]);strength=denoise if denoise is not None else settings['face_detail']['denoise']
            else:
                selected=copy.deepcopy(entry);selected['shot']['key_props']=[p for p in entry['shot']['key_props'] if prop_ids is None or p['prop_id'] in prop_ids]
                if prop_ids and set(prop_ids)-{p['prop_id'] for p in entry['shot']['key_props']}: raise ValueError('unknown region prop')
                tags=assemble_prompt(identity,selected,props=board['props'],config=config)['positive_tags'];strength=denoise if denoise is not None else settings['inpaint']['denoise']
                # Latent noise is constrained by a feathered mask, in addition to final pixel compositing.
                if not (assets/'mask.png').exists():
                    with Image.open(assets/'mask_full.png') as full_mask:
                        full_mask.crop(pixels).resize(tuple(settings[primitive]['resolution']),Image.Resampling.LANCZOS).save(assets/'mask.png')
            reference={'mechanism':'img2img','strength':strength,'uploaded_image':upload_reference(root,assets/'input.png',client),'identity_reference':face,'path':str((assets/'input.png').relative_to(root)),'sha256':digest(assets/'input.png')}
            config['resolutions'][framing]=[1024,1024]
            rec.setdefault('region',{'bbox':bbox,'pixels':pixels,'provenance':region_source,'input':reference,'mask':str((assets/'mask_full.png').relative_to(root)),'mask_sha256':digest(assets/'mask_full.png')})
        if primitive=='seed':
            original=next(a for a in state['attempts'] if a['shot_id']==shot_id and a['attempt_number']==rec['source_attempt'])
            if seed==original['seed']: raise ValueError('replacement seed must differ from source')
            tags=original.get('positive_tags',parts['positive_tags'])
            framing=original.get('framing',parts['framing'])
            reference=original.get('reference')
        def stage(stage_id, text, ref, kind):
            prior=next((a for a in state['attempts'] if a.get('repair_key')==key and a.get('repair_stage')==kind),None)
            if prior is None:
                # Build through the same runner API, then persist the full repair workflow before POST.
                old=state['config'];state['config']=config
                try: a=_new_attempt(root,state,stage_id,text,framing,generation_reference=ref)
                finally: state['config']=old
                a['repair_key']=key;a['repair_stage']=kind;a['repair_diagnosis']=rec['diagnosis']
                if primitive=='seed':
                    a['workflow']=copy.deepcopy(original['workflow'])
                    a['workflow']['7']['inputs']['seed']=seed
                    a['workflow']['9']['inputs']['filename_prefix']=a['filename_prefix']
                if primitive=='inpaint' and kind=='final':
                    uploaded=upload_reference(root,assets/'mask.png',client)
                    rec['region']['noise_mask']={'path':str((assets/'mask.png').relative_to(root)),'sha256':digest(assets/'mask.png'),'uploaded_image':uploaded}
                    a['workflow']['30']={'class_type':'LoadImageMask','inputs':{'image':uploaded,'channel':'red'}}
                    a['workflow']['31']={'class_type':'SetLatentNoiseMask','inputs':{'samples':['12',0],'mask':['30',0]}}
                    a['workflow']['7']['inputs']['latent_image']=['31',0]
                rec['stages'].append(a['filename_prefix']);_save(root,state)
                _process(root,state,a,client,recovering=False)
            else:
                a=prior
                if a['status'] not in ('completed','failed'): _process(root,state,a,client,recovering=True)
            if a['status']!='completed': raise RuntimeError('repair stage incomplete; resume same request')
            return a
        if primitive=='relayout':
            full=portrait_reference(root,portrait,'image',client,'qwen_edit',1.0)
            layout=stage(f'layout{shot_number:02d}',edit_instruction(entry,board['props']),full,'layout')
            reference={'mechanism':'img2img','strength':denoise if denoise is not None else config.get('layout_denoise',.55),'uploaded_image':upload_reference(root,root/layout['image']['path'],client),'identity_reference':face,'layout_image':layout['image'],'prompt_id':layout['prompt_id'],'upstream_reference':full}
        final=stage(shot_id,tags,reference,'final')
        if pixels and not final.get('raw_image'):
            final['raw_image']=copy.deepcopy(final['image']);output=root/(final['filename_prefix']+'_composite.png')
            with Image.open(root/rec['source_image']['path']) as base, Image.open(root/final['raw_image']['path']) as patch: composite_region(base,patch,pixels,12).save(output)
            with Image.open(output) as im: w,h=im.size
            final['image']={'path':output.name,'sha256':digest(output),'width':w,'height':h}
        rec['output']=copy.deepcopy(final['image'])
        if rec['status']!='reviewed': rec['status']='awaiting_review'
        rec['entry']=entry
        _save(root,state);write_repair_manifest(root,state)
        return rec


def write_repair_manifest(root,state):
    root=Path(root);base=json.loads((root/'manifest.json').read_text())
    source_manifest=root/'source_manifest.json'
    if source_manifest.exists():
        provenance=json.loads(source_manifest.read_text())
        for field in ('models','style','identity_file','storyboard_file','reference_models','reference_relationship'):
            if field in provenance: base[field]=provenance[field]
    base['source_manifest_file']='source_manifest.json'
    # runner writes its minimal manifest on downloads: reconstruct ordered story inputs.
    board=json.loads((root/'storyboard.json').read_text())
    base['images']=[{'shot_id':f'shot{e["number"]:02d}',**next(a['image'] for a in reversed(state['attempts']) if a['shot_id']==f'shot{e["number"]:02d}' and a['status']=='completed')} for e in board['shots']]
    base['repair_review_records']=[{'repair_key':k,**r['diagnosis']['result']} for k,r in state['repairs'].items() if r['diagnosis']['result']['status']=='reviewed']
    base['repairs']=state['repairs'];base['attempts']=state['attempts'];base['repair_source']=state['repair_source']
    log=root/'review_log.jsonl'
    calls=sum(json.loads(line)['event']=='request' for line in log.read_text().splitlines()) if log.exists() else 0
    base['usage']={'generated_images':sum(a['status']=='completed' and not a.get('inherited') for a in state['attempts']),
                   'submission_attempts':sum(a.get('post_count',0) for a in state['attempts'] if not a.get('inherited')),
                   'review_calls':calls,
                   'unresolved_submissions':sum(a.get('post_count',0)>0 and a['status'] not in ('completed','failed') for a in state['attempts'] if not a.get('inherited'))}
    state['repair_usage']=base['usage']
    save_json_atomic(root/'state.json',state)
    save_json_atomic(root/'manifest.json',base)


def record_result(root,key,review,observation):
    if not review or not isinstance(observation,str) or not observation.strip(): raise ValueError('real review and human observation required')
    with _job_lock(Path(root)):
        state=load_state(root);rec=state['repairs'][key]
        review_path=(Path(root)/review.get('file','')).resolve()
        if not review_path.is_relative_to(Path(root).resolve()) or not review_path.is_file():
            raise ValueError('review file missing or outside repair job')
        evidence=json.loads(review_path.read_text())
        if evidence.get('image',{}).get('sha256')!=rec['output']['sha256']:
            raise ValueError('review does not match repaired image')
        if review.get('sha256') and digest(review_path)!=review['sha256']:
            raise ValueError('review file hash mismatch')
        review={**review,'sha256':digest(review_path),'conclusion':evidence.get('conclusion')}
        rec['diagnosis']['result']={'status':'reviewed','review':review,'human_observation':observation};rec['status']='reviewed'
        for a in state['attempts']:
            if a.get('repair_key')==key:a['repair_diagnosis']=copy.deepcopy(rec['diagnosis'])
        _save(Path(root),state);write_repair_manifest(root,state)


def rewrite_composition(root, shot_number, diagnosis, llm):
    """Optional runtime rewrite; locked identity, outfit and scene never enter editable schema."""
    validate_diagnosis(diagnosis)
    from storycg.planning import SHOT
    from storycg.review import messages
    root=Path(root)
    board=json.loads((root/'storyboard.json').read_text())
    identity=json.loads((root/'identity.json').read_text())
    original=board['shots'][shot_number-1]
    answer=llm.call('repair_composition_rewrite',messages(
        'Revise only this shot composition to address the diagnosis. Preserve narrative event and registered prop IDs. '
        'Make camera, framing and contact positions readable. Do not add identity or appearance descriptions. '
        'Diagnosis: '+json.dumps(diagnosis,ensure_ascii=False)+'\nShot: '+json.dumps(original['shot'],ensure_ascii=False)+
        '\nRegistered props: '+json.dumps(board['props'],ensure_ascii=False)),SHOT)
    save_json_atomic(root/f'rewrite_shot{shot_number:02d}_{llm.session_id}.json',answer)
    if answer['status']!='pass': raise ValueError('composition rewrite failed')
    candidate=copy.deepcopy(original);candidate['shot']=answer['data']
    rewritten=copy.deepcopy(board);rewritten['shots'][shot_number-1]=candidate
    validate_storyboard(rewritten,len(board['shots']),identity)
    return candidate
