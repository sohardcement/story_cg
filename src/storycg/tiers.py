"""Evidence-only tier helpers. This module never submits ComfyUI jobs."""
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from storycg.identity_enrichment import load_rules
from storycg.state import save_json_atomic
from storycg.workflow import build_workflow


def normalize_tag(tag):
    return re.sub(r'\s+','_',tag.strip().lower())


def lora_names(object_info):
    return object_info['LoraLoader']['input']['required']['lora_name'][0]


def _frequencies(metadata):
    raw=metadata.get('ss_tag_frequency',{})
    if isinstance(raw,str):
        try:raw=json.loads(raw)
        except (ValueError,TypeError):return {}
    counts={}
    if not isinstance(raw,dict):return counts
    for dataset in raw.values():
        if not isinstance(dataset,dict):continue
        for tag,count in dataset.items():
            if isinstance(count,(int,float)) and count>=0:
                key=normalize_tag(tag)
                counts[key]=counts.get(key,0)+count
    return counts


def match_character_loras(character_tag, names, metadata, *, character_tags=None, rules=None):
    rules=rules or load_rules()
    target=normalize_tag(character_tag)
    known={normalize_tag(t) for t in (character_tags or [target])}
    known.add(target)
    matches=[];missing=[];rejected=[]
    for name in names:
        counts=_frequencies(metadata.get(name,{}))
        if not counts:
            missing.append(name);continue
        count=counts.get(target,0)
        total=sum(counts.get(tag,0) for tag in known)
        share=count/total if total else 0
        if count<rules['lora']['min_training_count'] or share<rules['lora']['min_character_share']:
            rejected.append({'name':name,'training_count':count,'character_share':share});continue
        matches.append({'name':name,'strength_model':rules['lora']['strength_model'],
                        'strength_clip':rules['lora']['strength_clip'],'trigger_words':[target],
                        'evidence':{'method':'exact normalized ss_tag_frequency match',
                                    'training_count':count,'character_share':share,
                                    'character_tag_universe':sorted(known),
                                    'scope':'dedicated only among supplied character tags',
                                    'filename_support':target.split('_(')[0] in normalize_tag(name),
                                    'metadata':metadata[name]}})
    matches.sort(key=lambda x:(-x['evidence']['character_share'],-x['evidence']['training_count'],
                               -int(x['evidence']['filename_support']),x['name']))
    return {'selected':matches[0] if matches else None,'matches':matches,'metadata_missing':missing,
            'rejected':rejected,'selection_rule':'character share, training count, filename support, lexical tie break'}


def determine_l1(character_tag, get_json, *, base_url='', character_tags=None, rules=None):
    """get_json(path, params) is a GET-only injected client; never trusts saved list as live evidence."""
    try:
        info=get_json('/object_info',None)
        names=lora_names(info)
        metadata={name:get_json('/view_metadata/loras',{'filename':name}) for name in names}
    except (httpx.HTTPError,OSError,ValueError,KeyError) as exc:
        return {'identity_tier':'tier_pending_comfyui','lora':None,
                'tier_evidence':{'status':'unavailable','error':str(exc),'method':'live object_info and view_metadata/loras'}}
    result=match_character_loras(character_tag,names,metadata,character_tags=character_tags,rules=rules)
    for match in result['matches']:
        match['evidence']['url']=str(httpx.URL(base_url+'/view_metadata/loras',params={'filename':match['name']}))
    if result['metadata_missing']:
        tier='tier_pending_comfyui'
    else:
        tier='L1' if result['selected'] else 'tier_pending_L2'
    return {'identity_tier':tier,'lora':result['selected'] if tier=='L1' else None,'tier_evidence':result}


def add_character_lora(workflow,lora):
    workflow=copy.deepcopy(workflow)
    # Insert after the two style LoRAs, preserving both MODEL and CLIP chains.
    workflow['30']={'class_type':'LoraLoader','inputs':{
        'model':['3',0],'clip':['3',1],'lora_name':lora['name'],
        'strength_model':lora['strength_model'],'strength_clip':lora['strength_clip']}}
    for key,node in workflow.items():
        if key=='30':continue
        for field,value in node['inputs'].items():
            if value==['3',0]:node['inputs'][field]=['30',0]
            elif value==['3',1]:node['inputs'][field]=['30',1]
    return workflow


def build_l2_workflow(config,identity,seed,prefix,rules=None):
    cfg=copy.deepcopy(config)
    cfg.pop('generation_reference',None)
    cfg.pop('character_lora',None)
    cfg['sampler']['denoise']=1.0
    rules=rules or load_rules()
    tags=', '.join(x for x in [identity['identity_block'],
                              ', '.join(x['tag'] for x in identity.get('default_outfit_tags',[])),
                              rules['neutral_l2_tags']] if x)
    workflow=build_workflow(cfg,tags,'medium',seed,prefix)
    return workflow


def download_references(character_tag,job_dir,client,*,rules=None):
    rules=rules or load_rules()
    cfg=rules['references']
    query=f'{character_tag} rating:g score:>={cfg["min_score"]} order:score'
    response=client.get('/posts.json',params={'tags':query,'limit':cfg['count']})
    response.raise_for_status()
    posts=response.json()
    directory=Path(job_dir)/'references'
    directory.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for post in posts:
        if (post.get('rating')!='g' or post.get('score',0)<cfg['min_score'] or
            character_tag not in post.get('tag_string_character','').split()):
            continue
        url=post.get('file_url')
        if not url:continue
        suffix=Path(urlparse(url).path).suffix.lower()
        if suffix not in ('.jpg','.jpeg','.png','.webp'):continue
        entry={'post_id':post['id'],'post_url':f'https://danbooru.donmai.us/posts/{post["id"]}',
               'url':url,'query_url':str(response.url),'rating':post['rating'],'score':post['score'],
               'purpose':'L2 comparison only; not original-design evidence','status':'pending'}
        manifest.append(entry)
        save_json_atomic(directory/'manifest.json',manifest)
        try:
            image=client.get(url);image.raise_for_status()
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(image.content)) as opened:opened.verify()
            filename=f'{post["id"]}{suffix}'
            (directory/filename).write_bytes(image.content)
            entry.update(path='references/'+filename,sha256=hashlib.sha256(image.content).hexdigest(),status='downloaded')
        except (httpx.HTTPError,OSError,ValueError) as exc:
            entry.update(status='failed',error=str(exc))
        save_json_atomic(directory/'manifest.json',manifest)
    save_json_atomic(directory/'manifest.json',manifest)
    return manifest


def identity_checklist(identity):
    mapping={'hair_color':'hair_color','eye_color':'eye_color','hair_length':'hairstyle',
             'hairstyle':'hairstyle','hair_details':'hairstyle','accessories':'accessories',
             'accessory_color':'accessories','features':'accessories'}
    return ([{'item':mapping.get(x.get('category'),'character'),'tag':x['tag']} for x in identity['appearance_tags']]
            +[{'item':'outfit','tag':x['tag']} for x in identity['default_outfit_tags']])


def evaluate_l2(checklist,comparisons,reliability):
    reliable=[];unverified=[]
    for expected in checklist:
        rows=[x for x in comparisons if x.get('item')==expected['item'] and x.get('tag')==expected['tag']]
        record={**expected,'comparisons':rows}
        if reliability.get('items',{}).get(expected['item'],{}).get('status')=='reliable':
            record['passed']=bool(rows) and all(x.get('status')=='pass' for x in rows)
            reliable.append(record)
        else:
            record['note']='未经可靠审核';unverified.append(record)
    passed=bool(reliable) and all(x['passed'] for x in reliable)
    return {'identity_tier':'L2' if passed else 'tier_pending_L3_L4',
            'reliable':reliable,'unverified':unverified,
            'limitations':'L2 evidence only covers calibrated reliable items'}


def prepare_l2(job_dir, identity, config, *, seeds=(1,), rules=None, reference_client=None):
    """Persist 1–2 unconditioned test workflows, without ever submitting them.

    A later generation runner must execute these exact workflows and supply the
    resulting image/workflow records to compare_l2. References are comparison only.
    """
    if not 1 <= len(seeds) <= 2:
        raise ValueError('L2 requires one or two tests')
    root=Path(job_dir)
    plan={'status':'awaiting_generation','generation_count':0,'vision_calls':0,
          'tests':[],'references':[],'checklist':identity_checklist(identity)}
    for number,seed in enumerate(seeds,1):
        path=f'l2/test_{number:02d}_workflow.json'
        workflow=build_l2_workflow(config,identity,seed,f'storycg_{root.name}_l2_{number:02d}_a001',rules)
        save_json_atomic(root/path,workflow)
        plan['tests'].append({'workflow_path':path,'seed':seed,'status':'not_submitted',
                              'workflow_sha256':hashlib.sha256((root/path).read_bytes()).hexdigest(),
                              'reference_injection':False})
    save_json_atomic(root/'l2/plan.json',plan)
    if reference_client is not None:
        plan['references']=download_references(identity['character_tag'],root,reference_client,rules=rules)
        save_json_atomic(root/'l2/plan.json',plan)
    return plan


def _local_file(root,relative):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('L2 artifact outside job directory')
    if not path.is_file():
        raise ValueError('missing L2 artifact')
    return path


def _plain_test_workflow(workflow):
    allowed={'CheckpointLoaderSimple','LoraLoader','CLIPTextEncode','EmptyLatentImage',
             'KSampler','VAEDecode','SaveImage'}
    if not workflow or any(n.get('class_type') not in allowed for n in workflow.values()):
        return False
    samplers=[n['inputs'] for n in workflow.values() if n['class_type']=='KSampler']
    return bool(samplers) and all(s.get('denoise')==1 and
        workflow.get(s.get('latent_image',[''])[0],{}).get('class_type')=='EmptyLatentImage' for s in samplers)


def compare_l2(job_dir,identity,test_images,references,reliability,vlm):
    """Compare every test/reference pair using the frozen VisionClient contract.

    test_images records contain path and workflow_path, from later real generation.
    No calls happen until references and unconditioned workflows are validated.
    Failures and missing pair results cannot pass.
    """
    from storycg.review import messages
    from jsonschema import Draft202012Validator
    root=Path(job_dir)
    checklist=identity_checklist(identity)
    evidence={'test_images':test_images,'references':references,'comparisons':[],
              'reference_purpose':'comparison only, not original-design evidence','vision_calls':0}
    failed={'identity_tier':'tier_pending_L3_L4','tier_evidence':evidence}
    if not test_images or len(test_images)>2 or not checklist:
        evidence['error']='missing test images or identity checklist';return failed
    refs=[r for r in references if r.get('status')=='downloaded']
    if not refs:
        evidence['error']='no downloaded reliable references';return failed
    try:
        paths=[]
        for record in test_images:
            image=_local_file(root,record['path'])
            workflow_path=_local_file(root,record['workflow_path'])
            workflow=json.loads(workflow_path.read_text())
            if not _plain_test_workflow(workflow):
                raise ValueError('L2 test workflow has conditioning or nonstandard nodes')
            character_lora = (identity.get('lora') or {}).get('name')
            if character_lora and any(n['class_type']=='LoraLoader' and
                                     n['inputs']['lora_name']==character_lora
                                     for n in workflow.values()):
                raise ValueError('L2 test contains character LoRA')
            paths.append(image)
        ref_paths=[]
        for ref in refs:
            path=_local_file(root,ref['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
                raise ValueError('reference hash mismatch')
            ref_paths.append(path)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        evidence['error']=str(exc);return failed
    grade={'type':'object','additionalProperties':False,'required':['status','reason'],
           'properties':{'status':{'enum':['pass','minor','fail','not_visible']},'reason':{'type':'string'}}}
    schema={'type':'object','additionalProperties':False,
            'properties':{f'c{i:04d}':grade for i in range(len(checklist))},
            'required':[f'c{i:04d}' for i in range(len(checklist))]}
    prompt=('Images are a Danbooru comparison reference followed by an unconditioned test image. '
            'For each checklist entry compare the visible test feature against the reference and expected tag. '
            'Do not infer invisible features. not_visible, minor, or fail cannot count as pass. '
            'Reference images must not add original-design descriptors. Checklist: '+
            json.dumps({f'c{i:04d}':item for i,item in enumerate(checklist)},ensure_ascii=False))
    all_rows=[]
    for number,test_path in enumerate(paths,1):
        for ref,path in zip(refs,ref_paths):
            pair_paths=[path,test_path]
            try:
                result=vlm.call(f'l2_test_{number}_reference_{ref["post_id"]}',messages(prompt,pair_paths),schema,pair_paths)
                evidence['vision_calls']+=1
                if result.get('status')!='pass':
                    raise ValueError(result.get('error','visual comparison failed'))
                Draft202012Validator(schema).validate(result['data'])
                rows=[{**expected,**result['data'][f'c{i:04d}']} for i,expected in enumerate(checklist)]
            except Exception as exc:
                rows=[{**expected,'status':'fail','reason':str(exc)} for expected in checklist]
                result={'status':'fail','error':str(exc)}
            all_rows.extend(rows)
            evidence['comparisons'].append({'test_path':str(test_path.relative_to(root.resolve())),
                                           'reference_post_id':ref['post_id'],'result':result,'items':rows})
    conclusion=evaluate_l2(checklist,all_rows,reliability)
    conclusion['tier_evidence']=evidence
    save_json_atomic(root/'l2/comparison.json',conclusion)
    return conclusion
