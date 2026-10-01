"""Persisted diagnosis/repair/audit loop. Frozen review functions are reused verbatim."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator
from storycg import review
from storycg.state import load_state, save_json_atomic
from storycg.reference import digest
from storycg.editing import EDIT_SCHEMA, state_descriptions, grounded_instruction
from storycg.planning import SHOT, validate_storyboard
from storycg.story_rules import validate_story_rules
from storycg.budget import Budget, BudgetExceeded, CycleClient, GuardedComfy, count_usage

CONFIG_PATH=Path(__file__).resolve().parents[2]/'config/cycle.json'


def settings():return json.loads(CONFIG_PATH.read_text())


def reliable_failures(results):
    return sum(item.get('status')=='fail' and item.get('reliability')=='reliable'
               for result in results for item in result.get('data',{}).values() if isinstance(item,dict))


def improved(before,after):
    if not after or any(r.get('status')!='pass' or not r.get('data') for r in after):return False
    before_keys={(i,k) for i,r in enumerate(before) for k in r.get('data',{})}
    after_keys={(i,k) for i,r in enumerate(after) for k in r.get('data',{})}
    if not before_keys.issubset(after_keys):return False
    return reliable_failures(after)<reliable_failures(before)


def repair_findings(audit):
    results=[audit['single'],audit['consistency']]
    count=audit.get('detection_items',{}).get('protagonist_count')
    if count:
        observed={**count,'status':'fail' if count['status']=='duplicate' else 'not_visible' if count['status']=='none_visible' else 'pass',
                  'observed_status':count['status'],'reliability':'unverified',
                  'reason':'local major face/person count; uncalibrated'}
        results.append({'status':'pass','data':{'protagonist_count':observed}})
    return results


def detection_improved(before,after):
    old=before.get('detection_items',{}).get('protagonist_count',{})
    new=after.get('detection_items',{}).get('protagonist_count',{})
    if old.get('status')!='duplicate' or new.get('status')!='ok':return False
    if new['protagonist_count']>=old['protagonist_count']:return False
    for layer in ('single','consistency'):
        prior=before[layer];current=after[layer]
        if current.get('status')!='pass' or not current.get('data'):return False
        if not prior.get('data',{}).keys() <= current['data'].keys():return False
        for key,value in current['data'].items():
            status=value.get('status');previous=prior.get('data',{}).get(key,{}).get('status')
            if status=='fail' and previous!='fail':return False
            if status=='not_visible' and previous!='not_visible':return False
    return True


def allowed_decision(decision, cfg=None):
    cfg=cfg or settings()
    if decision.get('primitive') not in cfg['problem_methods'].get(decision.get('problem'),[]):
        raise ValueError('primitive not allowed by problem mapping')
    return True


def stop_reason(rounds,stagnant,images,seconds,*,cost=1,cfg=None):
    cfg=cfg or settings()
    if rounds>=cfg['max_rounds_per_shot']:return 'round_limit'
    if stagnant>=cfg['max_consecutive_no_improvement']:return 'no_improvement'
    if images+cost>cfg['max_images']:return 'image_budget'
    if seconds>=cfg['max_minutes']*60:return 'time_budget'
    return None


def read(path):return json.loads(Path(path).read_text())


def usage(root):return count_usage(root)


def selected_attempt(state,number,selected=None):
    matches=[a for a in state['attempts'] if a['shot_id']==f'shot{number:02d}' and a['status']=='completed']
    return next((a for a in matches if a['attempt_number']==selected),matches[-1])


def issues(results):
    items=[]
    for r in results:
        if r.get('status')!='pass':items.append({'key':'audit_error','status':'fail','reason':r.get('error','unknown'),'reliability':'unverified'})
        for key,item in r.get('data',{}).items():
            if isinstance(item,dict) and item.get('status') in ('fail','minor','not_visible'):
                items.append({'key':key,**item})
    return items



def problem_key(key):
    mapped=review.reliability_key(key)
    if key.startswith('scene_'):return 'scene'
    if key=='body':return 'hands'
    if mapped=='character' or key=='signature_features':return 'hairstyle'
    return mapped


def validate_problem(problem,audit,narrative=False):
    if narrative and problem=='narrative':return
    observed={problem_key(i['key']) for i in issues(audit) if i['status']=='fail'}
    if any(i['key']=='signature_features' and i['status']=='fail' for i in issues(audit)):observed.add('accessories')
    if problem not in observed:raise ValueError('diagnosis problem does not match observed failures')


def candidate_improved(before,after):
    if not improved(before,after):return False
    for old,new in zip(before,after):
        for k,item in new.get('data',{}).items():
            if item.get('reliability')=='reliable' and item.get('status')=='not_visible' and old.get('data',{}).get(k,{}).get('status')!='not_visible':return False
            if review.reliability_key(k) in ('hair_color','outfit') and item.get('status')=='fail' and old.get('data',{}).get(k,{}).get('status')!='fail':return False
    return True


def validate_edit_target(problem,edit,state):
    grounded_instruction(edit,state)
    target=edit['target'];description=edit['description']
    if problem=='protagonist_count' and edit['operation']=='remove_duplicate_protagonist' and target=='entry.shot.action':return
    if problem=='props':
        if target.startswith('props.'):
            p=state['props'][int(target.split('.')[1])]
            if p['id'] in {r['prop_id'] for r in state['entry']['shot']['key_props']}:return
        if target.startswith('entry.shot.key_props.'):return
    if problem=='outfit' and target.startswith(('entry.outfit.tags.','identity.default_outfit_tags.')):return
    checks=review.identity_checklist(state['identity'],state['entry']['outfit'])
    keys=['accessories','hair_ornaments'] if problem=='accessories' else [problem]
    if problem=='accessories':
        allowed_extra={v['tag'] for v in state['identity'].get('appearance_tags',[]) if v.get('category') in ('accessories','accessory_color')}
    else:allowed_extra=set()
    allowed=allowed_extra | {v['tag'] for k in keys for v in checks.get(k,{}).get('expected',[]) if isinstance(v,dict) and 'tag' in v}
    if description in allowed and target.startswith(('identity.appearance_tags.','identity.default_outfit_tags.','entry.outfit.tags.')):return
    raise ValueError('edit target does not match diagnosed item')

def diagnosis_schema(cfg,*,state=None,problems=None):
    edit_schema=copy.deepcopy(EDIT_SCHEMA)
    if state is not None:
        descriptions=state_descriptions(state)
        edit_schema['properties']['target']={'enum':list(descriptions)}
        edit_schema['properties']['description']={'enum':list(dict.fromkeys(descriptions.values()))}
    return review.object_schema({
        'diagnosis':review.TEXT,'hypothesis':review.TEXT,'changes':review.TEXT,
        'problem':{'enum':problems or list(cfg['problem_methods'])},
        'primitive':{'enum':sorted({p for values in cfg['problem_methods'].values() for p in values})},
        'edit':{'anyOf':[edit_schema,{'type':'null'}]},
        'shot':{'anyOf':[SHOT,{'type':'null'}]},
        'target_shot':{'type':'integer','minimum':1}})


def cached_call(root,name,vlm,purpose,messages,schema,paths=()):
    path=root/'cycle_artifacts'/f'{name}.json'
    if path.exists():return read(path)
    result=vlm.call(purpose,messages,schema,paths)
    save_json_atomic(path,result)
    return result


def diagnose(root,number,audit,entry,board,identity,cycle,vlm,*,narrative=None):
    cfg=cycle['config'];slot=cycle['shots'][str(number)]
    label=f'decision_{number}_{len(slot["history"])+1}'+('_narrative' if narrative else '')+'_constraints2'
    locked_identity={k:identity[k] for k in ('identity_block','outfit_block','locked_tags') if k in identity}
    for key in ('appearance_tags','default_outfit_tags'):
        locked_identity[key]=[{k:v for k,v in item.items() if k in ('tag','category')} for item in identity.get(key,[])]
    state={'identity':locked_identity,'entry':entry,'props':board['props']}
    problems={problem_key(i['key']) for i in issues(audit) if i['status']=='fail'}
    if any(i['key']=='signature_features' and i['status']=='fail' for i in issues(audit)):problems.add('accessories')
    if narrative:problems.add('narrative')
    problems=sorted(problems & cfg['problem_methods'].keys())
    if not problems:raise ValueError('no mapped actionable FAIL item')
    content={'issues':issues(audit),'state':state,'allowed_methods':{key:permitted_methods(key,cycle,number) for key in problems},
             'exact_edit_descriptions':state_descriptions(state),'previous_repairs':[{'primitive':h['decision']['primitive'],'diagnosis':h['decision']['diagnosis'],'hypothesis':h['decision']['hypothesis'],'changes':h['decision']['changes'],'edit':h['decision'].get('edit'),'source_attempt':h['source_attempt'],'result':{k:h.get('result',{}).get(k) for k in ('improved','reliable_fails_before','reliable_fails_after','conclusion')}} for h in slot['history']]}
    if narrative:content['blind_comparison']=narrative;content['storyboard']=board
    base_message=(
        'Interpret actual review findings. Output one diagnosis and testable causal hypothesis, and one concrete corrective change. '
        'Choose ONLY from allowed_methods for the actual problem. Prefer instruction_edit for registered object number/state/color or small attribute/clothing drift. '
        'instruction_edit: supply edit with an exact key and exact value from exact_edit_descriptions, no invented descriptions; shot=null. '
        'relayout: edit=null; provide revised shot only, preserve event and registered prop IDs, improve visible cues/camera/contact. '
        'face_detail and inpaint: edit=null and shot=null; regions come from local detection only. Do not merely change seed or synonyms. '
        'target_shot must equal '+str(number)+'. '
        +('For narrative repair explain missing visible cue from image-only retelling and fix this target shot. ' if narrative else '')
        +json.dumps(content,ensure_ascii=False))
    feedback=''
    for revision in range(cfg.get('diagnosis_validation_retries',1)+1):
        name=label if revision==0 else label+f'_validation{revision}'
        result=cached_call(root,name,vlm,'cycle_llm_'+name,review.messages(base_message+feedback),diagnosis_schema(cfg,state=state,problems=problems))
        try:
            if result['status']!='pass':raise ValueError('diagnostic LLM failed: '+str(result.get('error')))
            return validate_decision(result['data'],number,audit,entry,board,identity,cycle,narrative=narrative)
        except (ValueError, __import__('jsonschema').ValidationError) as exc:
            save_json_atomic(root/'cycle_artifacts'/f'{name}_rejected.json',{'error':str(exc),'original_response_file':f'{name}.json'})
            feedback='\nThe script rejected the preceding diagnosis: '+str(exc)+'. Address a corresponding FAIL item, not minor or not_visible. Preserve registered state. Invalid preceding output: '+json.dumps(result.get('data'))
            if revision==cfg.get('diagnosis_validation_retries',1):raise



def permitted_methods(problem,cycle,number):
    methods=cycle['config']['problem_methods'][problem]
    if methods[0]=='instruction_edit' and not any(h['decision'].get('primitive')=='instruction_edit' and h['decision'].get('problem')==problem for h in cycle['shots'][str(number)]['history']):return methods[:1]
    if problem=='protagonist_count':return ['relayout']
    return methods

def validate_decision(decision,number,audit,entry,board,identity,cycle,*,narrative=None):
    cfg=cycle['config'];state={'identity':identity,'entry':entry,'props':board['props']}
    allowed_decision(decision,cfg)
    if decision['primitive'] not in permitted_methods(decision['problem'],cycle,number):raise ValueError('D6 method priority requires primary instruction editing before fallback')
    validate_problem(decision['problem'],audit,bool(narrative))
    if decision['target_shot']!=number:raise ValueError('diagnostic target mismatch')
    if decision['primitive']=='instruction_edit':validate_edit_target(decision['problem'],decision['edit'],state)
    elif decision['edit'] is not None:raise ValueError('only instruction_edit accepts edit')
    if decision['primitive']=='relayout':
        if decision['shot'] is None:raise ValueError('relayout needs revised composition')
        revised=copy.deepcopy(board);candidate=copy.deepcopy(entry);candidate['shot']=decision['shot'];revised['shots'][number-1]=candidate
        validate_storyboard(revised,len(board['shots']),identity)
        validate_story_rules(revised,allow_obscured=cycle.get('allow_obscured',False))
        # Tags-only WAI -> Qwen spatial conditioning is itself a substantive change.
        # It is permitted once for already-correct state, never repeated on the same source.
        for previous in cycle['shots'][str(number)]['history']:
            old=previous['decision']
            if previous.get('source_attempt')==cycle['shots'][str(number)]['best_attempt'] and old['primitive']=='relayout' and old.get('shot')==decision['shot']:
                raise ValueError('cannot repeat identical relayout on the same source')
        if {r['prop_id'] for r in candidate['shot']['key_props']}-{p['id'] for p in board['props']}:raise ValueError('unknown prop')
    elif decision['shot'] is not None:raise ValueError('only relayout can alter shot')
    return decision


def audit_candidate(root,number,image,entry,board,identity,config,vlm,label):
    path=root/'cycle_artifacts'/f'{label}.json'
    if path.exists():
        result=read(path)
        if result['image']['sha256']!=image['sha256']:raise ValueError('audit image mismatch')
        return result
    source=root/image['path']
    if digest(source)!=image['sha256']:raise ValueError('candidate hash mismatch')
    single=review.audit_single(vlm,source,entry,board['props'],config,label+'_single')
    from storycg.detection_review import detect_face_record, audit_detection
    face_path=root/'cycle_artifacts'/f'{label}_face.png'
    detected,face=detect_face_record(source,face_path)
    portrait=read(root/'portrait.json');paths=[root/portrait['image']['path'],source]
    if face:
        face['path']=str(face_path.relative_to(root))
        pair=root/'cycle_artifacts'/f'{label}_pair.png'
        # The reference pair also uses a local crop, including legacy portraits.
        ref_path=root/'cycle_artifacts'/f'{label}_reference_face.png'
        _,ref_face=detect_face_record(paths[0],ref_path)
        if ref_face:
            review.face_pair(ref_path,face_path,pair);paths.append(pair)
    detection_items=audit_detection(source,identity,entry,reference_path=paths[0])
    consistency=review.audit_consistency(vlm,paths,review.identity_checklist(identity,entry['outfit']),config,label+'_identity_outfit')
    result={'image':image,'single':single,'consistency':consistency,'detection':detected,'face':face,'detection_items':detection_items,
            'conclusion':review.conclude([*review.result_statuses(single),*review.result_statuses(consistency)])}
    save_json_atomic(path,result);return result


def neighbors(root,number,image,entry,board,cycle,config,vlm,label):
    output=root/'cycle_artifacts'/f'{label}_neighbors.json'
    if output.exists():return read(output)
    state=load_state(root);results=[]
    for other in (number-1,number+1):
        if 1<=other<=len(board['shots']):
            slot=cycle['shots'][str(other)];attempt=selected_attempt(state,other,slot['best_attempt'])
            previous=slot['entry']
            checks={**review.scene_checklist(entry['scene'],previous['scene']),
                    'outfit_continuity':{'expected':{'current':entry['outfit'],'adjacent':previous['outfit']},'source':'storyboard outfit states'}}
            result=review.audit_consistency(vlm,[root/attempt['image']['path'],root/image['path']],checks,config,label+f'_neighbor{other}',reference_note='参考=相邻镜头，检查服装与场景连续性')
            results.append({'neighbor':other,'image_sha256':attempt['image']['sha256'],**result})
    save_json_atomic(output,results);return results


def deliver(root,cycle,config,client):
    from storycg.pipeline import write_story_manifest,make_contact_sheet
    selected_board=read(root/'storyboard.json')
    if not (root/'initial_storyboard.json').exists():save_json_atomic(root/'initial_storyboard.json',selected_board)
    selected_board['shots']=[slot['entry'] for slot in cycle['shots'].values()]
    save_json_atomic(root/'storyboard.json',selected_board)
    manifest=write_story_manifest(root,config,client);state=load_state(root)
    manifest['images']=[];lines=['# 结果说明','']
    human_path=root/'human_observations.json'
    human=read(human_path) if human_path.exists() else {}
    portrait_path=root/'portrait.json'
    if portrait_path.exists():
        portrait=read(portrait_path)
        lines.extend(portrait.get('gate_notes',[]))
        if portrait.get('gate_policy')=='D7':
            lines.append('定妆门禁按 D7 聚合；冻结原始结论保留：'+portrait.get('raw_conclusion','unknown'))
    for number,slot in cycle['shots'].items():
        a=selected_attempt(state,int(number),slot['best_attempt'])
        manifest['images'].append({'shot_id':a['shot_id'],'attempt_number':a['attempt_number'],**a['image'], 'entry':slot['entry']})
        if slot.get('diagnosis_error'):lines.append('诊断被脚本拒绝：'+slot['diagnosis_error'])
        audit=slot['best_audit']
        final=cycle.get('final_review',{})
        singles=[r for r in final.get('single',[]) if r['shot']==int(number)]
        consistencies=[r for r in final.get('consistency',[]) if r['shot']==int(number)]
        faces=[r for r in final.get('faces',[]) if r['shot']==int(number)]
        if singles and consistencies:
            audit={**audit,'single':singles[0],'consistency':consistencies[0],'conclusion':review.conclude([*review.result_statuses(singles[0]),*review.result_statuses(consistencies[0]), *[r['status'] for r in faces]])}
        slot['final_conclusion']=audit['conclusion']
        lines.append(f'## 第{number}镜：{audit["conclusion"]}')
        lines.append(f'停止原因：{slot.get("stop","pending")}；选用 attempt {slot["best_attempt"]}。')
        for issue in issues([audit['single'],audit['consistency']]):
            note='' if issue.get('reliability')=='reliable' else '；该项未经可靠审核'
            lines.append(f'- {issue["key"]}: {issue["status"]} — {issue["reason"]}{note}')
        unreliable=[k for r in [audit['single'],audit['consistency']] for k,v in r.get('data',{}).items() if v.get('reliability')!='reliable']
        lines.append('该项未经可靠审核：'+', '.join(unreliable))
        for observation in human.get(number,[]):
            lines.append('- 人眼已知问题（不改冻结结论）：'+observation)
        for h in slot['history']:
            lines.append(f'- 修复 {h["round"]}: {h["decision"]["diagnosis"]} → {h["decision"]["hypothesis"]} → {h["decision"]["changes"]} → {h.get("result", "pending")}')
        lines.append('')
    manifest['usage']=usage(root);manifest['repair_cycle_file']='cycle.json';manifest['repair_history']=cycle['shots'];manifest['repairs']=state.get('repairs',{});manifest['attempts']=state['attempts']
    if cycle.get('final_review'):lines.append('整组冻结审核：'+cycle['final_review']['conclusion'])
    final=cycle.get('final_review',{})
    if final:
        blind=final.get('narrative_blind',{})
        lines.append('盲审调用：'+blind.get('status','unknown')+'；'+blind.get('error',''))
        for key,item in final.get('narrative_compare',{}).get('data',{}).items():
            if item.get('status')=='fail':lines.append('- 盲审 '+key+'：'+item.get('reason',''))
    if cycle.get('narrative_rejected'):lines.append('叙事修复诊断被拒绝，未提交新图：'+cycle['narrative_rejected']['result'])
    if cycle.get('blocked'):lines.append('阻塞：'+cycle['blocked'])
    save_json_atomic(root/'manifest.json',manifest)
    (root/'result.md').write_text('\n'.join(lines)+'\n')
    make_contact_sheet(root,root.parent.parent/'review')
    return state



def full_review(root,config,budget):
    calls=budget.settings['max_vision_calls']-budget.usage()['review_calls']
    owned=CycleClient(root,config['model_service_url'],budget=budget,max_calls=max(0,calls),max_seconds=budget.remaining_seconds())
    try:return review.review_job(root,config,client=owned)
    finally:owned.close()

def run_repair_cycle(job_dir,config,client):
    root=Path(job_dir).resolve();path=root/'cycle.json'
    identity=read(root/'identity.json');board=read(root/'storyboard.json')
    cycle=read(path) if path.exists() else {'version':1,'config':settings(),'shots':{},'phase':'initial_audit',
        'allow_obscured':config.get('allow_obscured_protagonist',False)}
    if not (root/'initial_storyboard.json').exists():save_json_atomic(root/'initial_storyboard.json',board)
    cfg=cycle['config']
    recovered=False
    for n,slot in cycle['shots'].items():
        label=f'decision_{n}_{len(slot["history"])+1}_validation1.json'
        modern=root/'cycle_artifacts'/f'decision_{n}_{len(slot["history"])+1}_constraints2.json'
        if slot.get('stop')=='diagnosis_rejected' and modern.exists():
            slot['validation_policy_revision']=2
        if slot.get('stop')=='diagnosis_rejected' and slot.get('validation_policy_revision',0)<2:
            slot.setdefault('rejected_decisions',[]).append({'stop':slot['stop'],'error':slot.get('diagnosis_error')})
            slot.pop('stop',None);slot.pop('diagnosis_error',None);slot['diagnosis_validation_resume']=True;slot['validation_policy_revision']=2;recovered=True
    if recovered:cycle['phase']='repairing';save_json_atomic(path,cycle)
    budget=Budget(root,cfg)
    client=GuardedComfy(client,budget)
    cycle.pop('blocked',None)
    def persist():save_json_atomic(path,cycle)
    def quota(cost=0,reserve_vision=0,reserve_llm=0):
        u=usage(root)
        if u['submission_attempts']+cost>cfg['max_images']:return 'image_budget'
        if u['elapsed_seconds']>=cfg['max_minutes']*60:return 'time_budget'
        if u['review_calls']+reserve_vision>cfg['max_vision_calls']:return 'vision_budget'
        if u['llm_calls']+reserve_llm>cfg['max_llm_calls']:return 'llm_budget'
        return None
    if cycle.get('phase')=='complete':return deliver(root,cycle,config,client)
    # Log counts are durable across processes. Text diagnosis calls have a separate prefix.
    u=usage(root)
    vlm=CycleClient(root,config['model_service_url'],budget=budget,max_calls=max(0,cfg['max_vision_calls']+cfg['max_llm_calls']-u['review_calls']-u['llm_calls']),
        max_seconds=max(0,cfg['max_minutes']*60-u['elapsed_seconds']))
    try:
        if not cycle['shots']:
            validate_story_rules(board,allow_obscured=cycle['allow_obscured'])
            state=load_state(root)
            for e in board['shots']:
                number=e['number'];a=selected_attempt(state,number)
                if quota(reserve_vision=3):raise ValueError('insufficient budget for initial audit')
                audit=audit_candidate(root,number,a['image'],e,board,identity,config,vlm,f'initial_{number}')
                cycle['shots'][str(number)]={'best_attempt':a['attempt_number'],'best_audit':audit,'entry':e,'history':[],'stagnant':0}
                persist()
        # Recover a partially completed initial audit as well.
        for e in board['shots']:
            number=e['number']
            if str(number) not in cycle['shots']:
                state=load_state(root);a=selected_attempt(state,number)
                audit=audit_candidate(root,number,a['image'],e,board,identity,config,vlm,f'initial_{number}')
                cycle['shots'][str(number)]={'best_attempt':a['attempt_number'],'best_audit':audit,'entry':e,'history':[],'stagnant':0};persist()
        cycle['phase']='repairing';persist()
        for number_text,slot in cycle['shots'].items():
            number=int(number_text)
            while not slot.get('stop') or (slot['history'] and slot['history'][-1].get('status')!='completed'):
                before=slot['best_audit'];results=repair_findings(before)
                pending=slot['history'][-1] if slot['history'] and slot['history'][-1].get('status')!='completed' else None
                if not pending:
                    actionable=[i for i in issues(results) if i['status']=='fail' and i['key']!='audit_error']
                    if not actionable:slot['stop']='no_actionable_fail';persist();break
                    u=usage(root);reason=stop_reason(len(slot['history']),slot['stagnant'],u['submission_attempts'],u['elapsed_seconds'],cfg=cfg) or quota(reserve_vision=7+3*len(board['shots'])+2,reserve_llm=1)
                    if u['elapsed_seconds']>=(cfg['max_minutes']-cfg.get('final_review_reserve_minutes',3))*60:reason='final_review_time_reserved'
                    if reason:slot['stop']=reason;persist();break
                    try:
                        decision=diagnose(root,number,results,slot['entry'],board,identity,cycle,vlm)
                    except (ValueError, __import__('jsonschema').ValidationError) as exc:
                        slot['stop']='diagnosis_rejected';slot['diagnosis_error']=str(exc);slot['validation_policy_revision']=2;persist();break
                    cost=2 if decision['primitive']=='relayout' else 1
                    if usage(root)['elapsed_seconds']>=(cfg['max_minutes']-cfg.get('final_review_reserve_minutes',8))*60:
                        slot['stop']='final_review_time_reserved';persist();break
                    if quota(cost,reserve_vision=7+3*len(board['shots'])+2):slot['stop']=quota(cost,reserve_vision=7+3*len(board['shots'])+2);persist();break
                    pending={'round':len(slot['history'])+1,'decision':decision,'status':'prepared','source_attempt':slot['best_attempt']}
                    slot['history'].append(pending);persist()
                try:
                    apply_round(root,number,slot,pending,board,identity,cycle,config,client,vlm)
                except ValueError as exc:
                    if not str(exc).startswith('non_subject_mask_ambiguous'):raise
                    pending.update(status='completed',skipped=True,error=str(exc))
                    slot['stop']='non_subject_mask_ambiguous'
                persist()
        if not quota(reserve_vision=3*len(board['shots'])+2):
            cycle['phase']='final_review';persist()
            fingerprint=hashlib.sha256(json.dumps([(n,slot['best_audit']['image']['sha256'],slot['entry']) for n,slot in cycle['shots'].items()],sort_keys=True).encode()).hexdigest()[:16]
            final_path=root/'cycle_artifacts'/f'final_review_{fingerprint}.json'
            deliver(root,cycle,config,client)
            if final_path.exists():report=read(final_path)
            else:
                report=full_review(root,config,budget);save_json_atomic(final_path,report)
            cycle['final_review']=report;persist()
            narrative=report.get('narrative_compare',{})
            bad=narrative.get('status')!='pass' or any(narrative.get('data',{}).get(k,{}).get('status')=='fail' for k in ('key_events','ending'))
            if bad and not cycle.get('narrative_attempted') and not quota(2,reserve_vision=3*len(board['shots'])+7,reserve_llm=2):
                # Runtime identifies the missing visible cue and target, within the same mapping.
                choose=cached_call(root,'narrative_target',vlm,'cycle_llm_narrative_target',review.messages(
                    'Identify one shot missing a visible causal cue for the key event/ending that the image-only retelling failed to read. Choose its number; do not claim it passed. '+json.dumps({'comparison':narrative,'board':board},ensure_ascii=False)),review.object_schema({'target_shot':{'type':'integer','minimum':1,'maximum':len(board['shots'])},'missing_cue':review.TEXT}))
                if choose['status']=='pass':
                    n=choose['data']['target_shot'];slot=cycle['shots'][str(n)]
                    if len(slot['history'])<cfg['max_rounds_per_shot'] and slot['stagnant']<cfg['max_consecutive_no_improvement']:
                        try:
                            decision=diagnose(root,n,[slot['best_audit']['single'],slot['best_audit']['consistency']],slot['entry'],board,identity,cycle,vlm,narrative={'comparison':narrative,'missing_cue':choose['data']['missing_cue']})
                            if decision['primitive']!='relayout':raise ValueError('narrative repair must change composition')
                        except (ValueError, __import__('jsonschema').ValidationError) as exc:
                            cycle['narrative_attempted']=True
                            cycle['narrative_rejected']={'diagnosis':choose['data'],'hypothesis':'visible cue correction must satisfy existing storyboard rules','changes':None,'result':str(exc)}
                            decision=None;persist()
                        if decision is not None:
                            pending={'round':len(slot['history'])+1,'decision':decision,'status':'prepared','source_attempt':slot['best_attempt'],'narrative':True}
                            cycle['narrative_attempted']=True;slot['history'].append(pending);persist()
                            apply_round(root,n,slot,pending,board,identity,cycle,config,client,vlm);persist()
                            deliver(root,cycle,config,client)
                            if pending['result']['improved']:
                                again=full_review(root,config,budget);save_json_atomic(root/'cycle_artifacts/narrative_final_review.json',again)
                                cycle['final_review']=again
                            else:
                                cycle['narrative_final_review_reused']='Selected images unchanged; retain original blind review without repeating calls.'
        else:cycle['final_review_skipped']=quota(reserve_vision=3*len(board['shots'])+2)
        cycle['phase']='complete'
        cycle['finished_at']=datetime.now(timezone.utc).isoformat()
        persist()
    except Exception as exc:
        cycle['blocked']=f'{type(exc).__name__}: {exc}';persist()
        if len(cycle['shots'])==len(board['shots']):deliver(root,cycle,config,client)
        raise
    finally:vlm.close()
    return deliver(root,cycle,config,client)


def apply_round(root,number,slot,pending,board,identity,cycle,config,client,vlm):
    from storycg.repairs import repair,record_result
    decision=pending['decision'];entry=slot['entry'];candidate=copy.deepcopy(entry)
    if decision['shot'] is not None:candidate['shot']=decision['shot']
    diagnosis={k:decision[k] for k in ('diagnosis','hypothesis','changes')};diagnosis['result']={'status':'pending'}
    kwargs={'repair_id':f'cycle{pending["round"]}', 'source_attempt':pending['source_attempt']}
    if decision['primitive']=='instruction_edit':kwargs['edit']=decision['edit']
    if decision['primitive']=='relayout':kwargs['revised_entry']=candidate
    if decision['primitive'] in ('face_detail','inpaint'):
        from storycg.detection_review import repair_mask
        from storycg.detect import detect_image
        source=root/slot['best_audit']['image']['path']
        purpose='remove_person' if decision['problem']=='protagonist_count' else 'face'
        region=repair_mask(source,None,purpose,root/'cycle_artifacts'/f'mask_{number}_{pending["round"]}')
        if region['provenance']['uncertain']:
            raise ValueError('non_subject_mask_ambiguous: merged segmentation requires experimental review')
        kwargs.update(bbox=region['bbox'],mask_path=region['mask_path'],mask_sha256=region['mask_sha256'],
            region_source=region['provenance'])
        if purpose=='remove_person':kwargs['operation']={'type':'remove_person'}
    label=f'repair_{number}_{pending["round"]}'
    before_neighbors=neighbors(root,number,slot['best_audit']['image'],entry,board,cycle,config,vlm,label+'_before')
    rec=repair(root,number,decision['primitive'],diagnosis,client,**kwargs)
    label=f'repair_{number}_{pending["round"]}'
    audited=audit_candidate(root,number,rec['output'],candidate,board,identity,config,vlm,label)
    adjacent=neighbors(root,number,rec['output'],candidate,board,cycle,config,vlm,label)
    before=[slot['best_audit']['single'],slot['best_audit']['consistency'],*before_neighbors];after=[audited['single'],audited['consistency'],*adjacent]
    visual_better=candidate_improved(before,after)
    count_better=decision['problem']=='protagonist_count' and detection_improved(slot['best_audit'],audited)
    better=(visual_better or count_better) and rec.get("boundary_check",{"passed":True})["passed"]
    if decision['problem']=='protagonist_count':
        recheck=audited.get('detection_items',{}).get('protagonist_count',{})
        better=better and recheck.get('status')=='ok'
        pending['count_recheck']=recheck
        pending['fallback_required']=recheck.get('status')=='duplicate'
    # Reliability of adjacent scene checks is unverified; retain all observations, never call them improvement.
    pending.update(status='completed',output=rec['output'],audit=audited,neighbors=adjacent,before_neighbors=before_neighbors,
        result={'reliable_fails_before':reliable_failures(before),'reliable_fails_after':reliable_failures(after),
                'improved':better,'improvement_basis':'uncalibrated_detection' if count_better else 'reliable_visual_metric','conclusion':audited['conclusion'],'unreliable_changes':issues(after)})
    key=f'shot{number:02d}_cycle{pending["round"]}'
    record_result(root,key,{'file':f'cycle_artifacts/{label}.json'},'Automatic audit record; human inspection pending, no human judgment inferred.')
    if better:
        state=load_state(root);a=next(a for a in state['attempts'] if a.get('repair_key')==key and a.get('repair_stage')=='final')
        slot.update(best_attempt=a['attempt_number'],best_audit=audited,entry=candidate,stagnant=0)
        board['shots'][number-1]=candidate
        save_json_atomic(root/'storyboard.json',board)
    else:slot['stagnant']+=1
