"""Diagnosed, restartable repair primitives; no automatic repair selection."""
import copy
import json
import math
import shutil
from pathlib import Path, PurePosixPath
from PIL import Image, ImageDraw, ImageFilter
from storycg.assembly import assemble_prompt
from storycg.controls import portrait_reference, upload_reference
from storycg.reference import digest, edit_instruction
from storycg.runner import _job_lock, _new_attempt, _process, _save, _now
from storycg.state import load_state, save_json_atomic
from storycg.planning import validate_storyboard

REPAIR_PRIMITIVES = ('face_detail','relayout','seed','instruction_edit')


def validate_diagnosis(record):
    if not isinstance(record, dict) or any(not record.get(k) for k in ('diagnosis','hypothesis','changes','result')):
        raise ValueError('repair requires diagnosis, hypothesis, changes and result (pending until audited)')
    for key in ('diagnosis','hypothesis','changes'):
        if not isinstance(record[key],str) or not record[key].strip():
            raise ValueError('diagnostic sections must be nonempty text')
    if not isinstance(record['result'],dict) or record['result'].get('status') not in ('pending','reviewed'):
        raise ValueError('result must explicitly be pending or reviewed')


def face_pixels(bbox, size, expansion=0):
    if len(bbox)!=4 or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=1 for v in bbox) or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]:
        raise ValueError('invalid normalized region')
    if not 0<=expansion<=2: raise ValueError('invalid expansion')
    w,h=size;dx=(bbox[2]-bbox[0])*expansion;dy=(bbox[3]-bbox[1])*expansion
    return [max(0,math.floor((bbox[0]-dx)*w)),max(0,math.floor((bbox[1]-dy)*h)),min(w,math.ceil((bbox[2]+dx)*w)),min(h,math.ceil((bbox[3]+dy)*h))]


def aspect_resolution(size, target_pixels=1024*1024):
    ratio=size[0]/size[1]
    # SDXL dimensions are multiples of 64; select closest aspect at bounded pixel count.
    candidates=[(w,h) for w in range(256,2049,64) for h in range(256,2049,64) if .8*target_pixels<=w*h<=1.2*target_pixels]
    return list(min(candidates,key=lambda wh:abs(math.log((wh[0]/wh[1])/ratio))+.05*abs(math.log(wh[0]*wh[1]/target_pixels))))


def boundary_check(base, composite, pixels, band=4, threshold=24):
    from PIL import ImageStat
    x0,y0,x1,y1=pixels
    checks=[]
    for inside,outside in [((x0,y0,min(x1,x0+band),y1),(max(0,x0-band),y0,x0,y1)),
                           ((max(x0,x1-band),y0,x1,y1),(x1,y0,min(base.width,x1+band),y1)),
                           ((x0,y0,x1,min(y1,y0+band)),(x0,max(0,y0-band),x1,y0)),
                           ((x0,max(y0,y1-band),x1,y1),(x0,y1,x1,min(base.height,y1+band)))]:
        if outside[0]>=outside[2] or outside[1]>=outside[3]: continue
        means=[ImageStat.Stat(im.convert('RGB').crop(box)).mean for im,box in [(composite,inside),(composite,outside),(base,inside),(base,outside)]]
        checks.append(max(abs(means[0][c]-means[1][c]-means[2][c]+means[3][c]) for c in range(3)))
    score=max(checks,default=0.)
    return {'passed':score<=threshold,'max_new_color_jump':score,'threshold':threshold,'edge_scores':checks}


def mask_boundary_check(base, composite, mask, threshold=24):
    import numpy as np
    cfg=json.loads((Path(__file__).resolve().parents[2]/'config/repairs.json').read_text())['face_detail']['seam']
    if base.size!=composite.size or mask.size!=base.size:raise ValueError('boundary images/mask sizes differ')
    before=np.asarray(base.convert('RGB'),dtype=float)
    after=np.asarray(composite.convert('RGB'),dtype=float)
    inside=np.asarray(mask.convert('L'))>=cfg['support_alpha']
    samples=[];scores=[]
    for axis in (0,1):
        cross=np.diff(inside.astype('int8'),axis=axis)!=0
        increment=np.maximum(0,np.abs(np.diff(after,axis=axis))-np.abs(np.diff(before,axis=axis))).max(axis=2)
        values=increment[cross];samples.extend(values.tolist())
        scores.append(float(np.percentile(values,cfg['percentile'])) if len(values) else 0.)
    score=float(np.percentile(samples,cfg['percentile'])) if samples else 0.
    return {'passed':score<=threshold,'max_new_color_jump':score,'threshold':threshold,
            'edge_scores':scores,'sampled_edges':len(samples),'support_alpha':cfg['support_alpha'],
            'percentile':cfg['percentile'],'method':'positive RGB gradient magnitude increment across outer mask support, one-pixel narrow band'}


def add_noise_mask(workflow, uploaded_image):
    # Use unused node IDs: L1's character LoRA already occupies node 30.
    available=max(80,max(map(int,workflow))+1)
    loader,noise=str(available),str(available+1)
    workflow[loader]={'class_type':'LoadImageMask','inputs':{'image':uploaded_image,'channel':'red'}}
    workflow[noise]={'class_type':'SetLatentNoiseMask','inputs':{'samples':workflow['7']['inputs']['latent_image'],'mask':[loader,0]}}
    workflow['7']['inputs']['latent_image']=[noise,0]


def feather_mask(size, pixels, feather):
    if feather<0: raise ValueError('negative feather')
    mask=Image.new('L',size,0); x0,y0,x1,y1=pixels
    if not (0<=x0<x1<=size[0] and 0<=y0<y1<=size[1]): raise ValueError('invalid pixel region')
    ImageDraw.Draw(mask).rectangle((x0,y0,x1-1,y1-1),fill=255)
    # Confine feathering to the specified region, preserving all outside pixels.
    blurred=mask.filter(ImageFilter.GaussianBlur(feather))
    from PIL import ImageChops
    return ImageChops.multiply(mask,blurred)


def composite_masked(base, patch, pixels, mask):
    if mask.size != base.size:raise ValueError('mask must match source image size')
    x0,y0,x1,y1=pixels
    layer=base.convert('RGB').copy()
    layer.paste(patch.convert('RGB').resize((x1-x0,y1-y0),Image.Resampling.LANCZOS),(x0,y0))
    # Context pixels are read for generation but only detector-selected pixels change.
    from PIL import ImageChops
    mask=ImageChops.multiply(mask.convert('L'),feather_mask(base.size,pixels,0))
    return Image.composite(layer,base.convert('RGB'),mask)


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
           denoise=None, seed=None, revised_entry=None, repair_id='repair1', edit=None, source_attempt=None, mask_path=None, mask_sha256=None):
    """Resume by repair_id; changing an existing request is rejected, never reposted."""
    validate_diagnosis(diagnosis)
    if primitive not in REPAIR_PRIMITIVES: raise ValueError('unsupported repair method: '+str(primitive))
    if primitive=='seed' and seed is None: raise ValueError('seed repair requires a seed')
    root=Path(root).resolve();shot_id=f'shot{shot_number:02d}'
    request={'primitive':primitive,'shot':shot_number,'diagnosis':diagnosis,'bbox':bbox,'region_source':region_source,'denoise':denoise,'seed':seed,'revised_entry':revised_entry, **({'edit':edit} if edit else {}), **({'source_attempt':source_attempt} if source_attempt is not None else {}), **({'mask_path':str(mask_path),'mask_sha256':mask_sha256} if mask_path else {})}
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
        if primitive=='instruction_edit':
            from storycg.editing import grounded_instruction
            instruction=grounded_instruction(edit,{'identity':identity,'entry':entry,'props':board['props']})
        if primitive=='face_detail':
            if not bbox or not region_source: raise ValueError('region and its provenance required')
            face_pixels(bbox,(1024,1024))
        if denoise is not None and not 0 < denoise <= 1:
            raise ValueError('denoise must be in (0,1]')
        records=state.setdefault('repairs',{}); key=f'{shot_id}_{repair_id}'
        if key in records:
            rec=records[key]
            stored={k:v for k,v in rec['request'].items() if k!='prop_ids' or v is not None}
            if stored!=request: raise ValueError('repair ID already has different inputs')
            if rec.get('output') and all(a['status']=='completed' for a in state['attempts'] if a.get('repair_key')==key):
                return rec  # Completed legacy requests must not acquire new stages under new selection rules.
        else:
            source=next(a for a in reversed(state['attempts']) if a['shot_id']==shot_id and a['status']=='completed' and (source_attempt is None or a['attempt_number']==source_attempt))
            rec={'request':request,'source_image':copy.deepcopy(source['image']),'source_attempt':source['attempt_number'],'diagnosis':copy.deepcopy(diagnosis),'stages':[], 'status':'prepared'}
            records[key]=rec;_save(root,state)
        settings=config.get('repair_settings') or json.loads((Path(__file__).resolve().parents[2]/'config/repairs.json').read_text())
        # Legacy face-detail snapshots stored shared seam settings under inpaint.
        face_settings={**settings.get('inpaint',{}),**settings['face_detail']}
        rec['settings']=settings
        parts=assemble_prompt(identity,entry,props=board['props'],config=config)
        negative=None
        face=portrait_reference(root,portrait,'face',client,'ipadapter',config['identity_control']['ipadapter_strength'])
        if seed is not None: config['generation_seed']=seed
        if primitive=='face_detail' and settings['face_detail'].get('cfg') is not None: config['sampler']['cfg']=settings['face_detail']['cfg']
        assets=root/'repair_assets'/key;assets.mkdir(parents=True,exist_ok=True)
        pixels=None;reference=face;tags=parts['positive_tags'];framing=parts['framing']
        if primitive=='face_detail':
            if not bbox or not region_source: raise ValueError('region and its provenance required')
            source_path=root/rec['source_image']['path']
            if digest(source_path)!=rec['source_image']['sha256']: raise ValueError('source hash mismatch')
            if primitive=='face_detail' and mask_path is None:
                from storycg.detection_review import repair_mask
                from storycg.detect import detect_image
                detected_region=repair_mask(source_path,detect_image(source_path),'face',assets/'detected')
                bbox=detected_region['bbox'];mask_path=detected_region['mask_path'];mask_sha256=detected_region['mask_sha256']
                region_source=detected_region['provenance']
            with Image.open(source_path) as im:
                full_mask=None
                if mask_path:
                    provided=Path(mask_path).resolve()
                    if not provided.is_relative_to(root):raise ValueError('detection mask outside repair job')
                    if not mask_sha256 or digest(provided)!=mask_sha256:raise ValueError('detection mask hash mismatch')
                    if region_source.get('source_sha256') != rec['source_image']['sha256']:
                        raise ValueError('detection mask source hash mismatch')
                    with Image.open(provided) as supplied:full_mask=supplied.convert('L')
                    if full_mask.size!=im.size or not full_mask.getbbox():raise ValueError('invalid detection mask geometry')
                    bounds=full_mask.getbbox()
                    bbox=[v/d for v,d in zip(bounds,[im.width,im.height,im.width,im.height])]
                pixels=face_pixels(bbox,im.size,settings['face_detail']['bbox_expansion'])
                crop=im.convert('RGB').crop(pixels)
                resolution=aspect_resolution(crop.size)
                if not (assets/'input.png').exists():
                    crop.resize(tuple(resolution),Image.Resampling.LANCZOS).save(assets/'input.png')
                if not (assets/'mask_full.png').exists():
                    (full_mask if full_mask is not None else feather_mask(im.size,pixels,settings[primitive]['feather_pixels'])).save(assets/'mask_full.png')
                if not (assets/'mask.png').exists():
                    with Image.open(assets/'mask_full.png') as stored_mask:
                        stored_mask.crop(pixels).resize(tuple(resolution),Image.Resampling.LANCZOS).save(assets/'mask.png')
            tags=', '.join([parts['identity_block'],parts['outfit_block'],'face, head, portrait, '+entry['shot']['expression']]);strength=denoise if denoise is not None else settings['face_detail']['denoise']
            reference={'mechanism':'img2img','strength':strength,'uploaded_image':upload_reference(root,assets/'input.png',client),'identity_reference':face,'path':str((assets/'input.png').relative_to(root)),'sha256':digest(assets/'input.png')}
            config['resolutions'][framing]=resolution
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
                try: a=_new_attempt(root,state,stage_id,text,framing,negative,generation_reference=ref)
                finally: state['config']=old
                a['repair_key']=key;a['repair_stage']=kind;a['repair_diagnosis']=rec['diagnosis']
                if primitive=='seed':
                    a['workflow']=copy.deepcopy(original['workflow'])
                    a['workflow']['7']['inputs']['seed']=seed
                    a['workflow']['9']['inputs']['filename_prefix']=a['filename_prefix']
                if primitive=='face_detail' and kind=='final':
                    uploaded=upload_reference(root,assets/'mask.png',client)
                    rec['region']['noise_mask']={'path':str((assets/'mask.png').relative_to(root)),'sha256':digest(assets/'mask.png'),'uploaded_image':uploaded}
                    add_noise_mask(a['workflow'],uploaded)
                rec['stages'].append(a['filename_prefix']);_save(root,state)
                _process(root,state,a,client,recovering=False)
            else:
                a=prior
                if a['status'] not in ('completed','failed'): _process(root,state,a,client,recovering=True)
            if a['status']!='completed': raise RuntimeError('repair stage incomplete; resume same request')
            return a
        if primitive=='instruction_edit':
            source_path=root/rec['source_image']['path']
            if digest(source_path)!=rec['source_image']['sha256']: raise ValueError('source hash mismatch')
            reference={'mechanism':'qwen_edit','strength':1.0,'uploaded_image':upload_reference(root,source_path,client),'path':rec['source_image']['path'],'sha256':rec['source_image']['sha256']}
            tags=instruction
            rec['instruction']=instruction
            rec['mandatory_reaudit']=['identity','outfit']
        if primitive=='relayout':
            full=portrait_reference(root,portrait,'image',client,'qwen_edit',1.0)
            layout=stage(f'layout{shot_number:02d}',edit_instruction(entry,board['props']),full,'layout')
            reference={'mechanism':'img2img','strength':denoise if denoise is not None else config.get('layout_denoise',.55),'uploaded_image':upload_reference(root,root/layout['image']['path'],client),'identity_reference':face,'layout_image':layout['image'],'prompt_id':layout['prompt_id'],'upstream_reference':full}
        final=stage(shot_id,tags,reference,'final')
        if pixels and not final.get('raw_image'):
            final['raw_image']=copy.deepcopy(final['image']);output=root/(PurePosixPath(final['filename_prefix']).name+'_composite.png')
            with Image.open(root/rec['source_image']['path']) as base, Image.open(root/final['raw_image']['path']) as patch, Image.open(assets/'mask_full.png') as mask:
                composite_masked(base,patch,pixels,mask).save(output)
            if primitive=='face_detail':
                with Image.open(root/rec['source_image']['path']) as base, Image.open(output) as merged, Image.open(assets/'mask_full.png') as mask:
                    rec['context_boundary_check']=boundary_check(base,merged,pixels,face_settings.get('boundary_band',4),face_settings.get('boundary_threshold',24))
                    rec['boundary_check']=mask_boundary_check(base,merged,mask,face_settings.get('boundary_threshold',24))
            with Image.open(output) as im: w,h=im.size
            final['image']={'path':output.name,'sha256':digest(output),'width':w,'height':h}
        rec['output']=copy.deepcopy(final['image'])
        if rec['status']!='reviewed': rec['status']='boundary_failed' if not rec.get('boundary_check',{'passed':True})['passed'] else 'awaiting_review'
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
    base['repairs']=state['repairs'];base['attempts']=state['attempts'];base['repair_source']=state.get('repair_source',str(root))
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
