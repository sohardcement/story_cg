"""Reference-conditioned identity recovery and honest unsupported outcomes."""
import copy
import hashlib
import json
import shutil
from pathlib import Path

from storycg.state import save_json_atomic


def needs_user(root, identity, reason, *, persist=True):
    identity=copy.deepcopy(identity)
    identity.update(status='needs_user',identity_tier='L4',needs_user=True,reason=reason,
                    question=reason+'。请提供参考图路径、角色 tag 或作品名；外貌资料仍需可追溯来源。',
                    user_can_provide=['参考图路径','角色 tag','作品名'])
    identity.setdefault('tier_evidence',{})['l4_reason']=reason
    if persist:save_json_atomic(Path(root)/'identity.json',identity)
    return identity


def import_user_reference(root, path):
    from PIL import Image
    root=Path(root).resolve();source=Path(path).expanduser().resolve()
    with Image.open(source) as im:im.verify()
    data=source.read_bytes();sha=hashlib.sha256(data).hexdigest()
    dest=root/'user_references'/(sha+source.suffix.lower())
    dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():dest.write_bytes(data)
    record={'path':str(dest.relative_to(root)),'sha256':sha,'original_path':str(source),
            'source':'用户提供','post_id':'user-'+sha[:12],'status':'downloaded',
            'purpose':'generation and comparison only; not original-design evidence','general_tags':[]}
    save_json_atomic(root/'user_references/manifest.json',[record])
    return record


def injection_available(config,client):
    required={'IPAdapterAdvanced':None,'LoadImage':None,
              'IPAdapterModelLoader':('ipadapter_file',config['reference_models']['ipadapter']),
              'CLIPVisionLoader':('clip_name',config['reference_models']['clip_vision'])}
    missing=[];sources=[]
    for node,item in required.items():
        url='/object_info/'+node
        response=client.http.get(url);response.raise_for_status();nodes=response.json()
        sources.append(config['comfyui_url']+url)
        if node not in nodes:missing.append(node)
        elif item and item[1] not in nodes[node]['input']['required'][item[0]][0]:missing.append(item[1])
    return {'available':not missing,'missing':missing,'mechanism':'ipadapter',
            'models':config['reference_models'],'sources':sources}


def _injected_workflow(workflow,record):
    ref=record.get('reference') or {}
    return (ref.get('mechanism')=='ipadapter' and
            workflow.get('12',{}).get('class_type')=='IPAdapterAdvanced' and
            workflow['12']['inputs']['weight']==ref.get('strength') and
            workflow['12']['inputs']['image']==['10',0] and
            workflow.get('10',{}).get('inputs',{}).get('image')==ref.get('uploaded_image') and
            workflow.get('7',{}).get('inputs',{}).get('model')==['12',0] and
            workflow['7']['inputs']['latent_image']==['6',0] and
            workflow['7']['inputs']['denoise']==1 and
            workflow.get('6',{}).get('class_type')=='EmptyLatentImage' and
            'style_lora_names' in record and
            len([n for n in workflow.values() if n.get('class_type')=='LoraLoader'])==len(record['style_lora_names']))


def compare_injected(root,identity,tests,refs,reliability,vlm):
    from storycg.tier_gate import compare_l2
    return compare_l2(root,identity,tests,refs,reliability,vlm,workflow_validator=_injected_workflow,
                      comparison_file='l3/comparison.json')


def comparison_available(comparison):
    evidence=comparison.get('tier_evidence',{})
    pairs=evidence.get('comparisons',[])
    return not evidence.get('error') and bool(pairs) and all(p.get('result',{}).get('status')=='pass' for p in pairs)


def l3_comparison_passes(comparison):
    if comparison.get('identity_tier')!='L2':return False
    # Unlike a tier knowledge probe, a setting portrait must also preserve
    # reliable clothing items when the reference's default subject is applicable.
    for row in comparison['tier_evidence'].get('outfit_comparisons',[]):
        active=[r for r in row['comparisons'] if r.get('applicable')]
        if row.get('reliability')!='unverified' and any(r.get('status')=='fail' for r in active):return False
        if row.get('reliability')=='reliable' and any(r.get('status')!='pass' for r in active):return False
    return True


def _face(root,path,vlm,purpose,dest):
    from storycg.detection_review import detect_face_record
    result,face=detect_face_record(path,root/dest)
    if face is None:raise ValueError('face_not_detected: 没有可定位的参考面部')
    face['path']=str(Path(face['path']).relative_to(root))
    return face,result


def current_l3_approval(approved):
    """An old affirmative result cannot bypass the current comparison policy."""
    from storycg.identity_enrichment import load_rules
    from storycg.tier_gate import evaluate_l2
    from storycg.tiers import identity_checklist
    evidence=approved.get('tier_evidence',{})
    comparison=evidence.get('l3',{}).get('comparison',{})
    if (evidence.get('comparison_version')!=load_rules()['tier_review']['version'] or
        comparison.get('tier_evidence',{}).get('comparison_version')!=load_rules()['tier_review']['version'] or
        not comparison_available(comparison)):
        return False
    rows=[row for pair in comparison['tier_evidence']['comparisons'] for row in pair.get('items',[])]
    if not rows:return False
    reliability=json.loads(Path('config/review_reliability.json').read_text())
    gate=evaluate_l2(identity_checklist(approved),rows,reliability)
    return l3_comparison_passes({'identity_tier':gate['identity_tier'],'tier_evidence':gate})


def l3_session(identity, references):
    """Keep resume stable within one model fingerprint, separate changed bytes."""
    from storycg.identity_enrichment import load_rules
    session_input={'comparison_version':load_rules()['tier_review']['version'],
        'references':[r['sha256'] for r in references], 'identity_block':identity['identity_block'],
        'outfit_tags':[r['tag'] for r in identity['default_outfit_tags']]}
    if identity.get('tier_model_fingerprint'):
        session_input['model_fingerprint']=identity['tier_model_fingerprint']
    return Path('l3')/hashlib.sha256(json.dumps(session_input,sort_keys=True).encode()).hexdigest()[:16]


def ensure_l3(root,identity,config,client,*,references,reference_root,vlm=None,force_probe=False):
    from storycg.identity_enrichment import load_rules
    from storycg.reference import cache_key,require_portrait
    from storycg.controls import upload_reference
    from storycg.runner import run_gen_one
    from storycg.review import VisionClient
    root=Path(root).resolve();reference_root=Path(reference_root).resolve()
    if (root/'portrait.json').exists():
        record=json.loads((root/'portrait.json').read_text())
        approved=record.get('approved_identity')
        if (not force_probe and record.get('status')=='pass' and record.get('gate_policy')=='L3 reliable comparison'
                and approved and approved.get('tier_model_fingerprint')==identity.get('tier_model_fingerprint')
                and current_l3_approval(approved)):
            if any(approved.get(key)!=identity.get(key) for key in ('character_tag','work_tag','identity_block','outfit_block','appearance_tags','default_outfit_tags','default_outfit_selection')):
                raise ValueError('approved L3 identity source mismatch')
            require_portrait(root)
            if record['cache_key']!=cache_key(approved,config):raise ValueError('approved L3 style mismatch')
            save_json_atomic(root/'identity.json',approved);return approved
        digest=hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()[:16]
        save_json_atomic(root/f'l3/history/portrait-{digest}.json',record)
    if identity.get('identity_tier')=='L3':
        return needs_user(root,identity,'已有定妆无法通过当前身份审核；需要重新验证参考注入')
    if identity.get('identity_tier')=='L4':return identity
    refs=[r for r in references if r.get('status')=='downloaded']
    if not refs:return needs_user(root,identity,'底模不认识且没有可用参考图')
    try:available=injection_available(config,client)
    except Exception as exc:
        return needs_user(root,identity,'参考注入机制无法核实：'+str(exc))
    save_json_atomic(root/'l3/mechanism.json',available)
    if not available['available']:return needs_user(root,identity,'参考注入机制不可用：'+', '.join(available['missing']))
    if not identity.get('appearance_tags'):return needs_user(root,identity,'没有可追溯的外貌 tag 资料')
    from storycg.tiers import identity_checklist
    reliability=json.loads(Path('config/review_reliability.json').read_text())
    if not any(row['item']!='outfit' and reliability.get('items',{}).get(row['item'],{}).get('status')=='reliable' for row in identity_checklist(identity)):
        return needs_user(root,identity,'缺少能可靠比对的来源外貌资料；无法验证参考注入结果')
    # The selected policy ranking is preserved. No visual cherry-picking.
    session=l3_session(identity,refs)
    copied=[]
    for r in refs:
        path=(reference_root/r['path']).resolve()
        if not path.is_relative_to(reference_root) or hashlib.sha256(path.read_bytes()).hexdigest()!=r['sha256']:
            return needs_user(root,identity,'参考图路径或 hash 无法验证')
        dest=root/session/'references'/path.name;dest.parent.mkdir(parents=True,exist_ok=True)
        if path!=dest:shutil.copyfile(path,dest)
        copied.append({**r,'path':str(dest.relative_to(root)),
                       'purpose':'generation and comparison only; not original-design evidence'})
    save_json_atomic(root/session/'references/manifest.json',copied)
    policy=load_rules()['l3']
    owned=vlm is None;vlm=vlm or VisionClient(root,config['model_service_url'],max_calls=policy['max_vision_calls'],max_seconds=policy['max_seconds'])
    attempt_path=root/session/'attempts.json'
    attempts=json.loads(attempt_path.read_text()) if attempt_path.exists() else []
    cfg=copy.deepcopy(config);cfg.pop('character_lora',None);cfg.pop('generation_reference',None)
    cfg['auto_retry_failed']=False;cfg['sampler']['denoise']=1
    cfg['prefix_namespace']=root.name+'_l3_'+session.name
    cfg['budget']['max_images_per_job']=policy['max_attempts']
    def finalize(item):
        number=item['number'];test=item['image'];comparison=item['comparison']
        image=root/test['path']
        try:
            output_face,bbox=_face(root,image,vlm,f'l3_portrait_bbox_{number}',str(session/f'face-{number}.png'))
        except (ValueError, RuntimeError, OSError) as exc:
            item['diagnostic_repair']['result']='face localization unavailable; reuse this image on resume, no regeneration'
            save_json_atomic(attempt_path,attempts)
            pending=copy.deepcopy(identity)
            pending['tier_evidence']['l3_pending']={'attempt':number,'image':test,'error':str(exc),
                'status':'awaiting_face_localization','attempts_file':str(attempt_path.relative_to(root))}
            save_json_atomic(root/'identity.json',pending)
            return pending
        approved=copy.deepcopy(identity);approved.update(identity_tier='L3',status='locked',needs_user=False)
        for field in ('reason','question','user_can_provide'):approved.pop(field,None)
        approved['tier_evidence'].pop('l3_pending',None)
        approved['tier_evidence']['l3']={'reference':copied[0],'comparison':comparison,'attempts_file':str(attempt_path.relative_to(root)),
           'approved_attempt':number,'mechanism':available,'weights':policy['weights'],'reference_purpose':'not original-design evidence'}
        record={'status':'pass','gate_policy':'L3 reliable comparison','cache_key':cache_key(approved,config),
            'source_task':str(root),'cache_hit':False,'image':test,'face':output_face,
            'audit_file':str(attempt_path.relative_to(root)),'approved_attempt':number,'approved_identity':approved,'limitations':comparison['tier_evidence'].get('limitations')}
        save_json_atomic(root/'portrait.json',record);require_portrait(root)
        save_json_atomic(root/'identity.json',approved)
        return approved

    try:
        for item in attempts:
            if l3_comparison_passes(item['comparison']):
                return finalize(item)
        face_path=root/session/'reference-face.json'
        if face_path.exists():face=json.loads(face_path.read_text())
        else:
            face,bbox=_face(root,root/copied[0]['path'],vlm,'l3_reference_bbox',str(session/'reference-face.png'))
            save_json_atomic(root/session/'reference-bbox.json',bbox);save_json_atomic(face_path,face)
        uploaded=upload_reference(root,root/face['path'],client,expected_sha256=face['sha256'])
        reliability=json.loads(Path('config/review_reliability.json').read_text())
        for number in range(len(attempts)+1,policy['max_attempts']+1):
            prior=attempts[-1]['comparison']['tier_evidence'] if attempts else {}
            appearance_failed=(any(not row.get('passed') for row in prior.get('reliable',[]))
                               or any(row.get('item')!='outfit' for row in prior.get('vetoes',[])))
            outfit_failures={row['tag'] for item in attempts for row in item['comparison']['tier_evidence'].get('outfit_comparisons',[])
                if any(r.get('applicable') and r.get('status')=='fail' for r in row['comparisons']) or
                   (row.get('reliability')=='reliable' and any(r.get('applicable') and r.get('status')!='pass' for r in row['comparisons']))}
            outfit_failures.intersection_update(row['tag'] for row in identity['default_outfit_tags'])
            strength=(policy['weights'][number-1] if not attempts or appearance_failed
                      else attempts[-1]['image']['reference']['strength'])
            reinforcement=[f'({tag}:{policy["outfit_weights"][number-1]})' for tag in sorted(outfit_failures)]
            cfg['generation_reference']={'mechanism':'ipadapter','strength':strength,'uploaded_image':uploaded,
                'path':face['path'],'sha256':face['sha256'],'source_reference':copied[0]}
            cfg['generation_seed']=load_rules()['tier_probe']['seed']
            diagnosis=(attempts[-1].get('comparison') if attempts else identity.get('tier_evidence'))
            repair={'diagnosis':diagnosis,'hypothesis':policy['diagnosis_reason'] if not attempts or appearance_failed else 'applicable outfit failed or reliable clothing unconfirmed; reinforce only sourced clothing while preserving face strength',
                    'changes':{'ipadapter_strength':strength,'reference_face':face,'sourced_outfit_reinforcement':reinforcement},'result':'awaiting comparison'}
            save_json_atomic(root/session/f'diagnosis-{number}.json',repair)
            tags=', '.join([identity['identity_block'],*(x['tag'] for x in identity['default_outfit_tags']),*cfg['portrait']['tags'],*reinforcement])
            state=run_gen_one(root/session/'generation',f'l3portrait{number}',tags,cfg['portrait']['framing'],cfg,client, generation_reference=cfg['generation_reference'])
            a=state['attempts'][-1]
            if a['status']!='completed':return identity  # reconcile this prompt on resume
            image=root/session/'generation'/a['image']['path'];wp=str(session/f'workflow-{number}.json')
            save_json_atomic(root/wp,a['workflow'])
            actual_reference=a.get('reference',cfg['generation_reference'])
            test={**a['image'],'path':str(image.relative_to(root)),'workflow_path':wp,
                  'reference':actual_reference,'prompt_id':a['prompt_id'],'seed':a['seed'],
                  'style_lora_names':[x['name'] for x in cfg['style_loras']]}
            comparison=compare_injected(root,identity,[test],copied,reliability,vlm)
            passed=l3_comparison_passes(comparison)
            item={'number':number,'image':test,'comparison':comparison,'diagnostic_repair':repair}
            repair['result']='comparison passed' if passed else 'failed; diagnose all vetoes and reliable unconfirmed items before next attempt' if number<policy['max_attempts'] else 'failed; downgrade L4'
            attempts.append(item);save_json_atomic(attempt_path,attempts)
            save_json_atomic(root/session/f'comparison-{number}.json',comparison)
            if not comparison_available(comparison):
                repair['result']='review unavailable; do not retry identity conditioning'
                save_json_atomic(attempt_path,attempts)
                return needs_user(root,identity,'参考注入后的审核不可用，无法确认身份；请恢复审核服务或提供更清晰的参考图')
            if passed:
                return finalize(item)
        last=attempts[-1]['comparison']['tier_evidence']
        failed=sorted({r['tag'] for r in last.get('reliable',[]) if not r.get('passed')})
        failed+=sorted({r['tag'] for r in last.get('vetoes',[])})
        failed+=sorted({r['tag'] for r in last.get('outfit_comparisons',[]) if r.get('reliability')=='reliable' and any(x.get('applicable') and x.get('status')!='pass' for x in r['comparisons'])})
        # Only exhausted, available failures establish model-specific L4 evidence.
        if (len(attempts)==policy['max_attempts'] and
                all(comparison_available(a['comparison']) and not l3_comparison_passes(a['comparison']) for a in attempts)):
            terminal={'status':'exhausted','model_fingerprint':identity.get('tier_model_fingerprint'),
                'session':str(session),'max_attempts':policy['max_attempts'],
                'attempts_file':str(attempt_path.relative_to(root)),
                'comparison_paths':[str(session/f'comparison-{a["number"]}.json') for a in attempts]}
            identity.setdefault('tier_evidence',{})['l3_terminal']=terminal
            save_json_atomic(root/session/'terminal.json',terminal)
        return needs_user(root,identity,'参考注入后仍无法还原关键特征：'+(', '.join(sorted(set(failed))) or '面部不可见或审核不可用'))
    except (ValueError,OSError,KeyError) as exc:
        return needs_user(root,identity,'参考注入验证不可用：'+str(exc))
    finally:
        if owned:vlm.close()
