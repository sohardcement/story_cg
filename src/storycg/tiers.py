"""Character tier matching, unconditioned probes, and evidence validation."""
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
    # Kohya caption prefixes are training identity tokens even when omitted from
    # ss_tag_frequency; count each subset once, never multiply by repeats.
    datasets=metadata.get('ss_datasets',[])
    if isinstance(datasets,str):
        try: datasets=json.loads(datasets)
        except (ValueError,TypeError): datasets=[]
    prefixes={}
    for dataset in datasets if isinstance(datasets,list) else []:
        for subset in dataset.get('subsets',[]):
            if subset.get('is_reg'): continue
            for tag in (subset.get('caption_prefix') or '').split(','):
                key=normalize_tag(tag)
                if key: prefixes[key]=prefixes.get(key,0)+subset.get('img_count',0)
    for key,count in prefixes.items(): counts[key]=max(counts.get(key,0),count)
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
        triggers=[tag for tag in counts if tag==target or tag.startswith(target+'_')]
        count=max((counts[tag] for tag in triggers),default=0)
        total=count+sum(counts.get(tag,0) for tag in known if tag!=target)
        share=count/total if total else 0
        if count<rules['lora']['min_training_count'] or share<rules['lora']['min_character_share']:
            rejected.append({'name':name,'training_count':count,'character_share':share});continue
        matches.append({'name':name,'strength_model':rules['lora']['strength_model'],
                        'strength_clip':rules['lora']['strength_clip'],'trigger_words':sorted(triggers),
                        'evidence':{'method':'normalized training tag or canonical caption prefix match',
                                    'training_count':count,'character_share':share,
                                    'character_tag_universe':sorted(known),
                                    'scope':'dedicated only among supplied character tags',
                                    'filename_support':target.split('_(')[0] in normalize_tag(name),
                                    'non_final_artifact':bool(re.search(rules['lora'].get('non_final_pattern', r'(?:eval|validation|checkpoint|intermediate|(?:step|epoch)[_ -]*\d+)'),name,re.I)),
                                    'metadata':metadata[name]}})
    matches.sort(key=lambda x:(-x['evidence']['character_share'],int(x['evidence']['non_final_artifact']),-x['evidence']['training_count'],
                               -int(x['evidence']['filename_support']),x['name']))
    return {'selected':matches[0] if matches else None,'matches':matches,'metadata_missing':missing,
            'rejected':rejected,'selection_rule':'character share, non eval/intermediate artifact, training count, filename support, lexical tie break'}


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
    if result['selected']:
        tier='tier_pending_L1_validation'
    elif result['metadata_missing']:
        tier='tier_pending_comfyui'
    else:
        tier='tier_pending_L2'
    return {'identity_tier':tier,'lora':result['selected'] if tier=='tier_pending_L1_validation' else None,'tier_evidence':result}


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


def knowledge_probe_tags(identity,rules=None):
    """D8b canonical knowledge input; sourced appearance never confirms itself."""
    rules=rules or load_rules()
    if not identity.get('character_tag'):
        raise ValueError('knowledge probe requires canonical character_tag')
    return ', '.join(filter(None,[identity['character_tag'],identity.get('work_tag'),rules['neutral_l2_tags']]))


def l1_validation_tags(identity,rules=None):
    """Validate the matched dedicated LoRA using the locked sourced identity."""
    rules=rules or load_rules()
    return ', '.join(filter(None,[identity['identity_block'],identity.get('outfit_block'),
                                 rules['neutral_l2_tags']]))


def build_l2_workflow(config,identity,seed,prefix,rules=None):
    cfg=copy.deepcopy(config)
    cfg.pop('generation_reference',None)
    cfg.pop('character_lora',None)
    cfg['sampler']['denoise']=1.0
    rules=rules or load_rules()
    tags=knowledge_probe_tags(identity,rules)
    workflow=build_workflow(cfg,tags,'medium',seed,prefix)
    return workflow


def _eyewear(tag,cfg):
    return tag in cfg.get('eyewear_tags',[]) or any(re.search(pattern,tag) for pattern in cfg.get('eyewear_patterns',[]))


def default_outfit_subject(identity):
    selected=identity.get('default_outfit_selection',{}).get('selected_base') or {}
    return selected.get('tag')


def reference_checklist(identity,reference):
    tags=set(reference.get('general_tags',[]))
    subject=default_outfit_subject(identity)
    applicable=bool(subject and subject in tags)
    return [{**row,'applicable':row['item']!='outfit' or applicable,
             'scope':'outfit_diagnostic' if row['item']=='outfit' else 'appearance',
             'affects_tier':row['item']!='outfit'} for row in identity_checklist(identity)]


def select_reference_posts(identity,posts,*,rules=None):
    rules=rules or load_rules();cfg=rules['references']
    appearance={x['tag'] for x in identity.get('appearance_tags',[])}
    outfit={x['tag'] for x in identity.get('default_outfit_tags',[])}
    locked_eyewear=any(_eyewear(tag,cfg) for tag in appearance)
    eligible=[];rejected=[];seen=set()
    for post in posts:
        if post['id'] in seen:continue
        seen.add(post['id']);tags=set(post.get('tag_string_general','').split());reasons=[]
        if post.get('rating')!='g':reasons.append('rating is not g')
        if post.get('score',0)<cfg['min_score']:reasons.append('below minimum score')
        if identity['character_tag'] not in post.get('tag_string_character','').split():reasons.append('character tag mismatch')
        if cfg.get('require_solo') and 'solo' not in tags:reasons.append('not solo')
        if cfg.get('require_full_body') and 'full_body' not in tags:reasons.append('not full body')
        excluded=sorted(tags.intersection(cfg.get('exclude_tags',[])))
        if excluded:reasons.append('excluded tags: '+', '.join(excluded))
        eyewear=sorted(tag for tag in tags if _eyewear(tag,cfg))
        if eyewear and not locked_eyewear:reasons.append('unlocked eyewear: '+', '.join(eyewear))
        url=post.get('file_url','')
        if not url or Path(urlparse(url).path).suffix.lower() not in ('.jpg','.jpeg','.png','.webp'):reasons.append('no supported original image')
        evidence={'post_id':post['id'],'score':post.get('score'), 'general_tags':sorted(tags),
                  'appearance_coverage':len(tags&appearance),'covered_appearance_tags':sorted(tags&appearance),
                  'outfit_coverage':len(tags&outfit),'covered_outfit_tags':sorted(tags&outfit),
                  'default_outfit_subject':default_outfit_subject(identity),
                  'outfit_applicable':bool(default_outfit_subject(identity) in tags)}
        if reasons:rejected.append({**evidence,'reasons':reasons})
        else:eligible.append((post,evidence))
    fields={'appearance_coverage_desc':lambda p,e:-e['appearance_coverage'],
            'default_outfit_coverage_desc':lambda p,e:-e['outfit_coverage'],
            'score_desc':lambda p,e:-p['score'],'post_id_asc':lambda p,e:p['id']}
    order=cfg['selection_order']
    eligible.sort(key=lambda pair:tuple(fields[name](*pair) for name in order))
    selected=eligible[:cfg['count']]
    return [p for p,e in selected],{'policy_version':cfg['policy_version'],'selection_order':order,
        'candidate_count':len(seen),'selected':[e for p,e in selected],
        'eligible_ranked':[e for p,e in eligible],'rejected':rejected}


def download_references(character_tag,job_dir,client,*,rules=None,identity=None):
    rules=rules or load_rules();cfg=rules['references']
    identity=identity or {'character_tag':character_tag,'appearance_tags':[],'default_outfit_tags':[]}
    query=f'{character_tag} rating:g score:>={cfg["min_score"]} order:score'
    if cfg.get('query_suffix'):query+=' '+cfg['query_suffix']
    posts=[];query_urls={};queries=[]
    for page in range(1,cfg.get('max_pages',1)+1):
        params={'tags':query,'limit':cfg.get('candidate_limit',cfg['count'])}
        if page>1:params['page']=page
        response=client.get('/posts.json',params=params);response.raise_for_status();batch=response.json()
        queries.append({'url':str(response.url),'response':batch})
        for post in batch:query_urls[post['id']]=str(response.url)
        posts.extend(batch)
        if len(batch)<params['limit']:break
    directory=Path(job_dir)/cfg.get('directory','references');directory.mkdir(parents=True,exist_ok=True)
    selected,audit=select_reference_posts(identity,posts,rules=rules)
    save_json_atomic(directory/'candidate-responses.json',queries)
    save_json_atomic(directory/'selection.json',audit)
    manifest=[]
    selection={row['post_id']:row for row in audit['selected']}
    for post in selected:
        url=post['file_url'];suffix=Path(urlparse(url).path).suffix.lower()
        entry={**selection[post['id']],'policy_version':cfg['policy_version'],
               'post_url':f'https://danbooru.donmai.us/posts/{post["id"]}',
               'url':url,'query_url':query_urls[post['id']],'rating':post['rating'],
               'purpose':'tier comparison only; not original-design evidence','status':'pending'}
        manifest.append(entry);save_json_atomic(directory/'manifest.json',manifest)
        try:
            response=client.get(url);response.raise_for_status()
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(response.content)) as opened:opened.verify()
            filename=f'{post["id"]}{suffix}';(directory/filename).write_bytes(response.content)
            entry.update(path=directory.name+'/'+filename,sha256=hashlib.sha256(response.content).hexdigest(),status='downloaded')
        except (httpx.HTTPError,OSError,ValueError) as exc:entry.update(status='failed',error=str(exc))
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
    reliable=[];unverified=[];outfit=[]
    for expected in checklist:
        rows=[x for x in comparisons if x.get('item')==expected['item'] and x.get('tag')==expected['tag']]
        record={**expected,'comparisons':rows}
        if expected['item']=='outfit':
            record['affects_tier']=False
            active=[r for r in rows if r.get('applicable',True) and r.get('status')!='not_applicable']
            record['passed']=all(r.get('status')=='pass' for r in active)
            record['applicable']=bool(active)
            record['reliability']=reliability.get('items',{}).get('outfit',{}).get('status','unverified')
            outfit.append(record);continue
        if reliability.get('items',{}).get(expected['item'],{}).get('status')=='reliable':
            record['passed']=bool(rows) and all(x.get('status')=='pass' for x in rows)
            reliable.append(record)
        else:
            record['note']='未经可靠审核';unverified.append(record)
    vetoes=[x for x in comparisons if x.get('item')!='outfit' and x.get('status')=='fail' and x.get('applicable',True)]
    not_visible=[x for x in comparisons if x.get('status')=='not_visible']
    passed=bool(reliable) and all(x['passed'] for x in reliable) and not vetoes
    return {'identity_tier':'L2' if passed else 'tier_pending_L3_L4',
            'reliable':reliable,'unverified':unverified,'outfit_comparisons':outfit,
            'vetoes':vetoes,'not_visible':not_visible,
            'limitations':'D8b: all reliable appearance items must pass; any appearance fail vetoes; outfit is diagnostic only; not_visible neither vetoes nor confirms'}


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
        plan['references']=download_references(identity['character_tag'],root,reference_client,rules=rules,identity=identity)
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


def compare_l2(job_dir,identity,test_images,references,reliability,vlm, *, character_lora=None, workflow_validator=None, comparison_file="l2/comparison-v3.json"):
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
            if not (workflow_validator(workflow,record) if workflow_validator else _plain_test_workflow(workflow)):
                raise ValueError('L2 test workflow has conditioning or nonstandard nodes')
            loaded=[n['inputs']['lora_name'] for n in workflow.values() if n['class_type']=='LoraLoader']
            allowed_loras=record.get('style_lora_names')
            if allowed_loras is not None:
                expected=list(allowed_loras)+([character_lora['name']] if character_lora else [])
                if sorted(loaded)!=sorted(expected):
                    raise ValueError('unexpected LoRA in tier test')
            named=(identity.get('lora') or {}).get('name')
            if not character_lora and named and named in loaded:
                raise ValueError('L2 test contains character LoRA')
            if character_lora:
                nodes=[n for n in workflow.values() if n['class_type']=='LoraLoader' and n['inputs']['lora_name']==character_lora['name']]
                if len(nodes)!=1 or any(nodes[0]['inputs'][k]!=character_lora[k] for k in ('strength_model','strength_clip')):
                    raise ValueError('L1 test missing configured character LoRA')
            paths.append(image)
        ref_paths=[]
        for ref in refs:
            path=_local_file(root,ref['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
                raise ValueError('reference hash mismatch')
            ref_paths.append(path)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        evidence['error']=str(exc);return failed
    grade={'type':'object','additionalProperties':False,
           'required':['status','reason','test_observation','reference_observation'],
           'properties':{'status':{'enum':['pass','minor','fail','not_visible']},
                         'reason':{'type':'string','minLength':1},
                         'test_observation':{'type':'string','minLength':1},
                         'reference_observation':{'type':'string','minLength':1}}}
    evidence['comparison_version']=load_rules()['tier_review']['version']
    evidence['tier_scope']=load_rules()['tier_review']['tier_scope']
    evidence['aggregation_policy']='D8b'
    evidence['probe_protocol']=('L3_reference_setting' if workflow_validator else
                                'L1_source_validation' if character_lora else 'L2_knowledge')
    all_rows=[]
    for number,test_path in enumerate(paths,1):
        for ref,path in zip(refs,ref_paths):
            reference_label='user-provided reference' if ref.get('source')=='用户提供' else 'Danbooru reference'
            entries=reference_checklist(identity,ref)
            active={f'c{i:04d}':row for i,row in enumerate(entries) if row['applicable']}
            schema={'type':'object','additionalProperties':False,
                    'properties':{key:grade for key in active},'required':list(active)}
            prompt=('Image 1 = generated test image. Image 2 = Danbooru reference. '
                    'Judge Image 1, never treat Image 2 as the generated test. '
                    'For each entry, first write test_observation from Image 1 only, '
                    'then reference_observation from Image 2 only, then compare against the sourced expected tag. '
                    'A reference may have a different outfit; do not attribute its clothing to Image 1. '
                    'Do not infer invisible features. not_visible, minor, or fail cannot count as pass. '
                    'Reference images must not add original-design descriptors. '
                    'Outfit entries apply because this reference is tagged '
                    'with the default clothing subject. Checklist: '+json.dumps(active,ensure_ascii=False))
            prompt=prompt.replace('Danbooru reference',reference_label)
            pair_paths=[test_path,path]
            message_list=messages(prompt,pair_paths)
            content=message_list[0]['content']
            content.insert(1,{'type':'text','text':'Image 1 = generated test image (judge this image).'})
            content.insert(3,{'type':'text','text':f'Image 2 = {reference_label} (comparison only).'})
            try:
                result=vlm.call(f'tier_v{load_rules()["tier_review"]["version"]}_test_{number}_reference_{ref["post_id"]}',message_list,schema,pair_paths)
                evidence['vision_calls']+=1
                if result.get('status')!='pass':raise ValueError(result.get('error','visual comparison failed'))
                Draft202012Validator(schema).validate(result['data'])
                rows=[{**row,**result['data'][key]} for key,row in active.items()]
            except Exception as exc:
                rows=[{**row,'status':'fail','reason':str(exc),
                       'test_observation':'comparison unavailable','reference_observation':'comparison unavailable'} for row in active.values()]
                result={'status':'fail','error':str(exc)}
            rows.extend({**row,'status':'not_applicable',
                         'reason':'reference tags do not cover the selected default clothing subject',
                         'test_observation':'not requested','reference_observation':'default clothing subject absent from reference tags'}
                        for row in entries if not row['applicable'])
            all_rows.extend(rows)
            evidence['comparisons'].append({'test_path':str(test_path.relative_to(root.resolve())),
                'reference_post_id':ref['post_id'],'image_roles':{'Image 1':'generated test image','Image 2':reference_label},
                'outfit_applicable':any(row['item']=='outfit' and row['applicable'] for row in entries),
                'result':result,'items':rows})
    conclusion=evaluate_l2(checklist,all_rows,reliability)
    if character_lora and conclusion['identity_tier']=='L2':
        conclusion['identity_tier']='L1'
    evidence['reliable']=conclusion['reliable']
    evidence['unverified']=conclusion['unverified']
    evidence['outfit_comparisons']=conclusion['outfit_comparisons']
    evidence['limitations']=conclusion['limitations']
    evidence['vetoes']=conclusion['vetoes']
    evidence['not_visible']=conclusion['not_visible']
    conclusion['tier_evidence']=evidence
    save_json_atomic(root/comparison_file,conclusion)
    return conclusion


def reaggregate_stored_tier(job_dir,identity,reliability):
    """Recompute D8b from immutable observations after checking probe provenance.

    Returns None for a wrong protocol or incomplete evidence. Never generates,
    downloads, or calls a reviewer. The caller retains the raw comparison files.
    """
    from storycg.fallback import comparison_available
    root=Path(job_dir).resolve();evidence=identity.get('tier_evidence',{})
    if not comparison_available({'tier_evidence':evidence}):return None
    if not 1<=len(evidence.get('test_images',[]))<=2:return None
    if not any(r.get('status')=='downloaded' for r in evidence.get('references',[])):return None
    lora=identity.get('lora')
    try:
        probe=_local_directory(root,evidence['artifact_root'])
        for test in evidence['test_images']:
            image=_local_file(probe,test['path'])
            if hashlib.sha256(image.read_bytes()).hexdigest()!=test['sha256']:return None
            graph=json.loads(_local_file(probe,test['workflow_path']).read_text())
            if not _plain_test_workflow(graph):return None
            loaded=[n['inputs'] for n in graph.values() if n['class_type']=='LoraLoader']
            expected=list(test['style_lora_names'])+([lora['name']] if lora else [])
            if sorted(n['lora_name'] for n in loaded)!=sorted(expected):return None
            positives=[n['inputs']['text'] for n in graph.values() if n['class_type']=='CLIPTextEncode']
            prefix=evidence['generation_config']['positive_prefix']
            tags=l1_validation_tags(identity) if lora else knowledge_probe_tags(identity)
            text=prefix+', '+tags
            if lora:
                chosen=evidence['lora_match']['selected']
                if chosen!=lora:return None
                checked=match_character_loras(identity['character_tag'],[lora['name']],
                    {lora['name']:lora['evidence']['metadata']},
                    character_tags=lora['evidence']['character_tag_universe'])
                if not checked['selected']:return None
                node=next(n for n in loaded if n['lora_name']==lora['name'])
                if any(node[k]!=lora[k] for k in ('strength_model','strength_clip')):return None
                if lora.get('trigger_words'):text+=', '+', '.join(lora['trigger_words'])
            if text not in positives:return None
        for ref in evidence['references']:
            if ref.get('status')=='downloaded' and hashlib.sha256(_local_file(probe,ref['path']).read_bytes()).hexdigest()!=ref['sha256']:return None
        rows=[r for pair in evidence['comparisons'] for r in pair['items']]
        checklist=identity_checklist(identity)
        for pair in evidence['comparisons']:
            if any(not any(r['item']==c['item'] and r['tag']==c['tag'] for r in pair['items']) for c in checklist if c['item']!='outfit'):return None
        result=evaluate_l2(checklist,rows,reliability)
    except (KeyError,ValueError,OSError,TypeError,StopIteration):return None
    if lora and result['identity_tier']=='L2':result['identity_tier']='L1'
    new=copy.deepcopy(identity);new['identity_tier']=result['identity_tier']
    new['tier_evidence'].update({k:v for k,v in result.items() if k!='identity_tier'})
    new['tier_evidence'].update(aggregation_policy='D8b',
        probe_protocol='L1_source_validation' if lora else 'L2_knowledge',
        original_comparison_version=evidence.get('original_comparison_version',evidence.get('comparison_version')),
        comparison_version=load_rules()['tier_review']['version'],tier_scope='appearance_only')
    return new


def _local_directory(root,relative):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_dir():raise ValueError('tier artifacts outside job directory or missing')
    return path


def tier_generation_config(config,identity):
    tier=identity.get('identity_tier','pending')
    if tier not in ('L1','L2','L3'):
        raise ValueError(f'identity tier pending: {tier}; no story generation')
    cfg=copy.deepcopy(config)
    cfg.pop('generation_reference',None)
    cfg.pop('character_lora',None)
    cfg['identity_control']={**cfg.get('identity_control',{}),**copy.deepcopy(load_rules()['generation_identity_control'])}
    # Optional Qwen layout stays configured; probes strip every image input separately.
    if tier=='L1':
        if not identity.get('lora'):
            raise ValueError('L1 missing character LoRA evidence')
        cfg['character_lora']=copy.deepcopy(identity['lora'])
    return cfg


def live_inventory(config,client,reference_client,job_dir):
    """Read every advertised LoRA; classify training tags with Danbooru categories."""
    from datetime import datetime,timezone
    info=client.http.get('/object_info/LoraLoader');info.raise_for_status()
    names=lora_names(info.json())
    from storycg.model_metadata import cached_lora_metadata
    metadata,metadata_status=cached_lora_metadata(config,client,names)
    tags=sorted({t for m in metadata.values() for t in _frequencies(m)})
    categories=[]
    for offset in range(0,len(tags),80):
        response=reference_client.get('/tags.json',params={'search[name_comma]':','.join(tags[offset:offset+80]),'limit':100})
        response.raise_for_status()
        categories.append({'url':str(response.url),'response':response.json()})
    result={'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'object_info_url':config['comfyui_url']+'/object_info/LoraLoader',
            'names':names,'metadata':metadata,'metadata_status':metadata_status,'categories':categories,
            'character_tags':sorted({t['name'] for r in categories for t in r['response'] if t['category']==4})}
    save_json_atomic(Path(job_dir)/'tier/inventory.json',result)
    return result


def ensure_identity_tier(job_dir,identity,config,client,*,inventory=None,reference_client=None,vlm=None,allow_l3=True):
    """One persistent probe. Pending results stop; no blind seed retries."""
    from storycg.identity import USER_AGENT,BASE_URL
    from storycg.runner import run_gen_one
    from storycg.review import VisionClient
    root=Path(job_dir).resolve()
    if identity.get('status')!='locked':return identity
    from storycg.fallback import ensure_l3,needs_user,import_user_reference,comparison_available
    if identity.get('identity_tier') not in ('L1','L2','L3','L4') and not (root/'portrait.json').exists():
        from storycg.reference import reuse_l3_portrait
        cached=reuse_l3_portrait(root,identity,tier_generation_config(config,dict(identity,identity_tier='L3')))
        if cached:return cached
    probe=root/'tier'/f'probe_v{load_rules()["tier_review"]["version"]}_{root.name}'
    opts=json.loads((root/'job.json').read_text()).get('generation_options',{}) if (root/'job.json').exists() else {}
    try:user_ref=import_user_reference(root,opts['reference_path']) if opts.get('reference_path') else None
    except (ValueError,OSError) as exc:return needs_user(root,identity,'用户参考图无法读取：'+str(exc))
    rules=load_rules()
    evidence=identity.get('tier_evidence',{})
    if evidence.get('validation_completed') and evidence.get('aggregation_policy')!='D8b':
        if identity.get('identity_tier')=='L3':
            from storycg.fallback import current_l3_approval
            if current_l3_approval(identity):
                identity['tier_evidence']['aggregation_policy']='D8b'
                save_json_atomic(root/'identity.json',identity)
                return identity
        else:
            reliability=json.loads(Path('config/review_reliability.json').read_text())
            migrated=reaggregate_stored_tier(root,identity,reliability)
            if migrated:
                identity=migrated
                save_json_atomic(root/'identity.json',identity)
    if (identity.get('tier_evidence',{}).get('validation_completed') and
            identity['tier_evidence'].get('comparison_version')==rules['tier_review']['version'] and
            identity['tier_evidence'].get('aggregation_policy')=='D8b'):
        probe=root/identity['tier_evidence'].get('artifact_root',str(probe.relative_to(root)))
        if identity.get('identity_tier')=='tier_pending_L3_L4' and allow_l3:
            refs_path=probe/load_rules()['references']['directory']/'manifest.json'
            refs=[user_ref] if user_ref else json.loads(refs_path.read_text()) if refs_path.exists() else []
            return ensure_l3(root,identity,config,client,references=refs,reference_root=root if user_ref else probe,vlm=vlm)
        return identity
    if identity.get('tier_evidence'):
        previous={'identity_tier':identity.get('identity_tier'),'tier_evidence':identity['tier_evidence']}
        digest=hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest()[:16]
        save_json_atomic(root/f'tier/history/{digest}.json',previous)
    if reference_client is None:
        with httpx.Client(base_url=BASE_URL,timeout=30,trust_env=False,headers={'User-Agent':USER_AGENT}) as owned:
            return ensure_identity_tier(root,identity,config,client,inventory=inventory,reference_client=owned,vlm=vlm,allow_l3=allow_l3)
    try:
        inventory=inventory or live_inventory(config,client,reference_client,root)
        match=match_character_loras(identity['character_tag'],inventory['names'],inventory['metadata'],
                                    character_tags=inventory['character_tags'],rules=rules)
        match['inventory_source']={'object_info_url':inventory['object_info_url'],'retrieved_at':inventory['retrieved_at']}
        save_json_atomic(root/'tier/lora-match.json',match)
        lora=match['selected']
        identity['lora']=lora
        identity['identity_tier']='tier_pending_L1_validation' if lora else 'tier_pending_L2'
        identity['tier_evidence']={'lora_match':match,'status':'awaiting_probe'}
        save_json_atomic(root/'identity.json',identity)
        protocol_suffix='_l1_source_validation' if lora else ''
        probe=root/'tier'/f'probe_v{rules["tier_review"]["version"]}_{root.name}{protocol_suffix}'
        refs_path=probe/rules['references'].get('directory','references')/'manifest.json'
        references=[] if user_ref else json.loads(refs_path.read_text()) if refs_path.exists() else download_references(identity['character_tag'],probe,reference_client,rules=rules,identity=identity)
        if user_ref:
            from shutil import copyfile
            dest=probe/'user_reference'/Path(user_ref['path']).name;dest.parent.mkdir(parents=True,exist_ok=True)
            copyfile(root/user_ref['path'],dest)
            references=[{**user_ref,'path':str(dest.relative_to(probe))}]
        if len([r for r in references if r.get('status')=='downloaded'])<(1 if user_ref else 2):
            return needs_user(root,identity,'底模身份未验证且没有足够的可靠参考图')
        cfg=copy.deepcopy(config)
        cfg.pop('generation_reference',None);cfg.pop('character_lora',None)
        cfg['identity_control']={'mechanism':'none'};cfg['layout_mode']='none'
        cfg['sampler']['denoise']=1.0
        cfg['generation_seed']=rules['tier_probe']['seed']
        cfg['budget']['max_images_per_job']=rules['tier_probe']['max_tests']
        cfg['budget']['max_job_minutes']=rules['tier_probe']['max_seconds']/60
        cfg['auto_retry_failed']=False
        if lora:cfg['character_lora']=lora
        tags=l1_validation_tags(identity,rules) if lora else knowledge_probe_tags(identity,rules)
        state=run_gen_one(probe,'tier01',tags,rules['tier_probe']['framing'],cfg,client)
        attempt=state['attempts'][-1]
        if attempt['status']!='completed':
            identity['tier_evidence'].update(status='generation_pending',attempt_status=attempt['status'],prompt_id=attempt.get('prompt_id'))
            save_json_atomic(root/'identity.json',identity);return identity
        workflow_path='test-workflow.json'
        save_json_atomic(probe/workflow_path,attempt['workflow'])
        tests=[{**attempt['image'],'workflow_path':workflow_path,'prompt_id':attempt['prompt_id'],
                'seed':attempt['seed'],'reference_injection':False,
                'style_lora_names':[x['name'] for x in cfg['style_loras']]}]
        reliability=json.loads(Path('config/review_reliability.json').read_text())
        owned_vision=vlm is None
        vlm=vlm or VisionClient(probe,config['model_service_url'],max_calls=rules['tier_probe']['max_vision_calls']+rules['l3']['max_vision_calls'],max_seconds=rules['tier_probe']['max_seconds']+rules['l3']['max_seconds'])
        try:
            result=compare_l2(probe,identity,tests,references,reliability,vlm,character_lora=lora)
            identity['identity_tier']=result['identity_tier']
            identity['tier_evidence']={**result['tier_evidence'],'lora_match':match,'validation_completed':True,
                                       'artifact_root':str(probe.relative_to(root)),'generation_count':state['stats']['generated_images'],
                                       'generation_config':cfg}
            save_json_atomic(root/'identity.json',identity)
            if result['identity_tier']=='tier_pending_L3_L4':
                if not comparison_available(result):
                    return needs_user(root,identity,'L2 审核不可用，无法确认底模是否认识该角色；请恢复审核服务或提供可用参考图')
                if allow_l3:
                    return ensure_l3(root,identity,config,client,references=references,reference_root=probe,vlm=vlm)
            return identity
        finally:
            if owned_vision:vlm.close()
    except (httpx.HTTPError,OSError,ValueError,KeyError) as exc:
        identity['identity_tier']='tier_pending_resources'
        identity.setdefault('tier_evidence',{}).update(status='unavailable',error=f'{type(exc).__name__}: {exc}')
        save_json_atomic(root/'identity.json',identity)
        return identity
