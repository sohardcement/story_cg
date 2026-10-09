"""Persisted diagnosis/repair/audit loop. Frozen review functions are reused verbatim."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator
from storycg import review
from storycg.closed_eyes import requires_closed_eyes, drop_eye_colour_check, mark_eye_colour_not_applicable
from storycg.state import load_state, save_json_atomic
from storycg.reference import digest
from storycg.editing import EDIT_SCHEMA, state_descriptions, grounded_instruction
from storycg.planning import SHOT, validate_storyboard
from storycg.story_rules import validate_story_rules
from storycg.budget import Budget, BudgetExceeded, CycleClient, GuardedComfy, count_usage
from storycg.v1_scope import is_prop_item

CONFIG_PATH=Path(__file__).resolve().parents[2]/'config/cycle.json'
# Calibration keys stay distinct; repair routing groups findings by the state to fix.
REPAIR_PROBLEM_ALIASES={'outfit_detail_vs_portrait':'outfit'}
DIAGNOSIS_POLICY_REVISION=4
# D29: compare reliable failures lexicographically, never by weighted sums.
REPAIR_FAILURE_TIER_NAMES=('protagonist_count','identity','outfit','action_composition_framing_scene_narrative')
REPAIR_IDENTITY_ITEMS=frozenset(('character','hair_color','hairstyle','eye_color',
    'signature_features','hair_ornaments','accessories','eye_color_sampled',
    'accessory_color_sampled','accessories_zoom','hair_ornaments_zoom'))


def settings():return json.loads(CONFIG_PATH.read_text())


def reliable_failures(results):
    return sum(item.get('status')=='fail' and item.get('reliability')=='reliable'
               for result in results for item in result.get('data',{}).values() if isinstance(item,dict))


def failure_tier(key):
    if key in ('protagonist_count','extra_body_part'):return 0  # V2-16: same level as the count gate (D29)
    if key in REPAIR_IDENTITY_ITEMS:return 1
    if review.reliability_key(key)=='outfit' or key=='outfit_detail_vs_portrait':return 2
    return 3


def reliable_failure_vector(results):
    counts=[0]*len(REPAIR_FAILURE_TIER_NAMES)
    for result in results:
        for key,item in result.get('data',{}).items():
            if isinstance(item,dict) and item.get('status')=='fail' and item.get('reliability')=='reliable':
                counts[failure_tier(key)]+=1
    return tuple(counts)


def improved(before,after):
    if not after or any(r.get('status')!='pass' or not r.get('data') for r in after):return False
    before_keys={(i,k) for i,r in enumerate(before) for k in r.get('data',{})}
    after_keys={(i,k) for i,r in enumerate(after) for k in r.get('data',{})}
    if not before_keys.issubset(after_keys):return False
    return reliable_failure_vector(after)<reliable_failure_vector(before)


def repair_findings(audit):
    results=[audit['single'],audit['consistency']]
    detection = audit.get('detection_items',{})
    if detection.get('review'):
        return [*results, detection['review']]
    count=detection.get('protagonist_count')
    if count:
        observed={**count,'status':'fail' if count['status']=='duplicate' else 'not_visible' if count['status']=='none_visible' else 'pass',
                  'observed_status':count['status'],'reliability':'unverified',
                  'reason':'local major face/person count; uncalibrated'}
        results.append({'status':'pass','data':{'protagonist_count':observed}})
    return results


def comparison_findings(audit):
    """Include calibrated local counts, including passes, in candidate selection."""
    return repair_findings(audit)


def allowed_decision(decision, cfg=None):
    from storycg.repairs import REPAIR_PRIMITIVES
    if decision.get('primitive') not in REPAIR_PRIMITIVES:
        raise ValueError('unsupported repair method: '+str(decision.get('primitive')))
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
    if key in REPAIR_PROBLEM_ALIASES:return REPAIR_PROBLEM_ALIASES[key]
    if key=='extra_body_part':return key
    mapped=review.reliability_key(key)
    if key.startswith('scene_'):return 'scene'
    if key=='body':return 'hands'
    if mapped=='character' or key=='signature_features':return 'hairstyle'
    return mapped


def actionable_issues(audit):
    return [i for i in issues([r for r in audit if r.get('status')=='pass'])
            if i['status']=='fail' and i.get('reliability')=='reliable' and not is_prop_item(i['key'])]


def actionable_problems(audit,cfg,*,narrative=False):
    findings=actionable_issues(audit)
    problems={problem_key(i['key']) for i in findings}
    if any(i['key']=='signature_features' for i in findings):problems.add('accessories')
    if narrative:problems.add('narrative')
    missing={i['key'] for i in findings if not cfg['problem_methods'].get(problem_key(i['key']))}
    if narrative and not cfg['problem_methods'].get('narrative'):missing.add('narrative')
    if missing:raise ValueError('unmapped reliable FAIL items: '+', '.join(sorted(missing)))
    return sorted(problems)


def attempted_problems(slot):
    return {problem_key(h['decision']['problem']) for h in slot['history'] if not h.get('skipped')}


def preferred_problems(audit,cycle,number,*,narrative=False):
    observed=actionable_problems(audit,cycle['config'],narrative=narrative)
    untried=set(observed)-attempted_problems(cycle['shots'][str(number)])
    return sorted(untried) if untried else observed


def validate_problem(problem,audit,narrative=False):
    if narrative and problem=='narrative':return
    findings=actionable_issues(audit)
    observed={problem_key(i['key']) for i in findings}
    if any(i['key']=='signature_features' for i in findings):observed.add('accessories')
    if problem not in observed:raise ValueError('diagnosis problem does not match observed failures')


def regression_records(before,after):
    """V2-14 (返工 1): reliable pass -> minor/fail items, each with its tier and whether it vetoes.

    A regression vetoes an improvement only when its tier is at or above the decisive tier, the first
    failure-vector position that changed (D29: count > identity > outfit > action/composition). A lower-tier
    regression never vetoes a higher-tier fix, as with not_visible in candidate_improved.
    """
    old_vector=reliable_failure_vector(before);new_vector=reliable_failure_vector(after)
    decisive=next((i for i,(old,new) in enumerate(zip(old_vector,new_vector)) if old!=new),None)
    records=[]
    for old,new in zip(before,after):
        for key,item in new.get('data',{}).items():
            prior=old.get('data',{}).get(key)
            if (isinstance(item,dict) and isinstance(prior,dict) and prior.get('status')=='pass'
                    and item.get('reliability')=='reliable' and item.get('status') in ('minor','fail')):
                tier=failure_tier(key)
                records.append({'item':key,'from':'pass','to':item['status'],
                    'tier':REPAIR_FAILURE_TIER_NAMES[tier],
                    'decisive_tier':None if decisive is None else REPAIR_FAILURE_TIER_NAMES[decisive],
                    'vetoes':decisive is not None and tier<=decisive})
    return records


def reliable_regressions(before,after):
    """Readable form of regression_records, each marked as vetoing or not."""
    return [f"{r['item']}: {r['from']} → {r['to']}（可靠项退化，{'否决改善' if r['vetoes'] else '不否决'}）"
            for r in regression_records(before,after)]


def candidate_improved(before,after):
    if not improved(before,after):return False
    if any(record['vetoes'] for record in regression_records(before,after)):return False
    old_vector=reliable_failure_vector(before);new_vector=reliable_failure_vector(after)
    decisive=next(i for i,(old,new) in enumerate(zip(old_vector,new_vector)) if old!=new)
    for old,new in zip(before,after):
        for k,item in new.get('data',{}).items():
            # Do not call hiding the decisive or a higher tier a repair. Lower
            # tier abstentions, like lower tier failures, cannot veto a priority fix.
            if isinstance(item,dict) and item.get('reliability')=='reliable' and item.get('status')=='not_visible' and failure_tier(k)<=decisive and old.get('data',{}).get(k,{}).get('status')!='not_visible':return False
    return True


def validate_edit_target(problem,edit,state):
    grounded_instruction(edit,state)
    target=edit['target'];description=edit['description']
    if problem=='protagonist_count' and edit['operation']=='remove_duplicate_protagonist' and target=='entry.shot.action':return
    if problem=='outfit' and target.startswith(('entry.outfit.tags.','identity.default_outfit_tags.')):return
    checks=review.identity_checklist(state['identity'],state['entry']['outfit'])
    keys=['accessories','hair_ornaments'] if problem=='accessories' else [problem]
    if problem=='accessories':
        allowed_extra={v['tag'] for v in state['identity'].get('appearance_tags',[]) if v.get('category') in ('accessories','accessory_color')}
    else:allowed_extra=set()
    allowed=allowed_extra | {v['tag'] for k in keys for v in checks.get(k,{}).get('expected',[]) if isinstance(v,dict) and 'tag' in v}
    if description in allowed and target.startswith(('identity.appearance_tags.','identity.default_outfit_tags.','entry.outfit.tags.')):return
    raise ValueError('edit target does not match diagnosed item')


def diagnosis_state(identity,entry,props):
    locked={k:identity[k] for k in ('identity_block','outfit_block','locked_tags') if k in identity}
    for key in ('appearance_tags','default_outfit_tags'):
        locked[key]=[{k:v for k,v in item.items() if k in ('tag','category')} for item in identity.get(key,[])]
    return {'identity':locked,'entry':entry}


def edit_choices(problem,state):
    choices=[]
    for target,description in state_descriptions(state).items():
        for operation in EDIT_SCHEMA['properties']['operation']['enum']:
            edit={'operation':operation,'target':target,'description':description}
            try:validate_edit_target(problem,edit,state)
            except (ValueError,__import__('jsonschema').ValidationError):continue
            choices.append(edit)
    return choices


def diagnosis_schema(cfg,*,state=None,problems=None):
    from storycg.repairs import REPAIR_PRIMITIVES
    edit_schema=copy.deepcopy(EDIT_SCHEMA)
    if state is not None:
        descriptions=state_descriptions(state)
        edit_schema['properties']['target']={'enum':list(descriptions)}
        edit_schema['properties']['description']={'enum':list(dict.fromkeys(descriptions.values()))}
    schema=review.object_schema({
        'diagnosis':review.TEXT,'hypothesis':review.TEXT,'changes':review.TEXT,
        'problem':{'enum':problems or list(cfg['problem_methods'])},
        'primitive':{'enum':sorted({p for values in cfg['problem_methods'].values() for p in values if p in REPAIR_PRIMITIVES})},
        'edit':{'anyOf':[edit_schema,{'type':'null'}]},
        'shot':{'anyOf':[SHOT,{'type':'null'}]},
        'target_shot':{'type':'integer','minimum':1}})
    if state is not None:
        schema['allOf']=[]
        for problem in problems or list(cfg['problem_methods']):
            choices=[{'const':edit} for edit in edit_choices(problem,state)]
            schema['allOf'].append({'if':{'properties':{'problem':{'const':problem}}},
                'then':{'properties':{'edit':{'anyOf':[{'type':'null'},*choices]}}}})
    return schema


def cached_call(root,name,vlm,purpose,messages,schema,paths=()):
    path=root/'cycle_artifacts'/f'{name}.json'
    if path.exists():return read(path)
    result=vlm.call(purpose,messages,schema,paths)
    save_json_atomic(path,result)
    return result


def permitted_methods(problem,cycle,number):
    from storycg.repairs import REPAIR_PRIMITIVES
    methods=[method for method in cycle['config']['problem_methods'][problem] if method in REPAIR_PRIMITIVES]
    if not methods:raise ValueError('no supported repair method for problem '+problem)
    if problem=='protagonist_count':
        # TASK-v2 §2.2-3: a reliable duplicate count re-rolls the seed first,
        # before any other repair. Only while no count repair was attempted.
        attempted=any(h['decision'].get('problem')=='protagonist_count' and not h.get('skipped')
                      for h in cycle['shots'][str(number)]['history'])
        if 'seed' in methods and not attempted:
            return ['seed']
        methods=[method for method in methods if method!='seed']
        if any(h['decision'].get('primitive')=='instruction_edit' and h['decision'].get('problem')=='protagonist_count'
               for h in cycle['shots'][str(number)]['history']):
            methods=[method for method in methods if method!='instruction_edit']
    if methods[0]=='instruction_edit' and not any(h['decision'].get('primitive')=='instruction_edit' and h['decision'].get('problem')==problem for h in cycle['shots'][str(number)]['history']):return methods[:1]
    return methods

def validate_decision(decision,number,audit,entry,board,identity,cycle,*,narrative=None):
    cfg=cycle['config'];state={'identity':identity,'entry':entry,'props':board['props']}
    allowed_decision(decision,cfg)
    if decision['primitive'] not in permitted_methods(decision['problem'],cycle,number):raise ValueError('D6 method priority requires primary instruction editing before fallback')
    validate_problem(decision['problem'],audit,bool(narrative))
    if decision['problem'] not in preferred_problems(audit,cycle,number,narrative=bool(narrative)):
        raise ValueError('diagnosis problem does not match untried actionable priority')
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
    elif decision['shot'] is not None:raise ValueError('only relayout can alter shot')
    return decision


def consistency_review(vlm,paths,identity,entry,config,purpose):
    """Identity/outfit consistency review. V2-14 (D58): a closed-eye shot has no eye colour to judge."""
    checks=review.identity_checklist(identity,entry['outfit'])
    eyes_not_applicable=drop_eye_colour_check(checks,entry['shot'],config)
    consistency=review.audit_consistency(vlm,paths,checks,config,purpose)
    if eyes_not_applicable:mark_eye_colour_not_applicable(consistency)
    return consistency


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
    detection_items=audit_detection(source,identity,entry,reference_path=paths[0], review_config=config)
    consistency=consistency_review(vlm,paths,identity,entry,config,label+'_identity_outfit')
    from storycg import extra_part
    if extra_part.should_check(detection_items):  # V2-16: one vision question, skipped on a reliable duplicate count
        consistency=extra_part.merge_into(consistency,extra_part.check(vlm,source,identity,label+'_extra_body_part'))
    # Use the same supplementary reviewer as delivery before selecting a repair.
    # Otherwise a reliable detailed clothing failure first appears after the loop.
    from storycg import review_v3
    if review_v3.enabled(config):
        if entry['outfit'].get('change_at_shot') is None:
            outfit_ref=paths[0]
        else:
            state=load_state(root)
            selected_paths=[root/selected_attempt(state,e['number'])['image']['path'] for e in board['shots']]
            selected_paths[number-1]=source
            outfit_ref=review_v3.outfit_reference(number-1,board['shots'],selected_paths,paths[0])
        prepared=review_v3.prepare(source,outfit_ref,identity,entry,root/'cycle_artifacts'/f'{label}_v3',items=review_v3.enabled_items(config))
        supplementary=review_v3.audit(vlm,prepared,config,label+'_v3')
        consistency['v3']=supplementary
        if supplementary['status']!='pass':
            consistency['status']='fail';consistency['error']=supplementary.get('error','v3 audit failed')
        else:consistency.setdefault('data',{}).update(supplementary['data'])
    result={'image':image,'single':single,'consistency':consistency,'detection':detected,'face':face,'detection_items':detection_items,
            'conclusion':review.conclude([*review.result_statuses(single),*review.result_statuses(consistency),*review.result_statuses(detection_items['review'])])}
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
    notes=review.reliability_note_lines(cycle.get('final_review'),config)
    if notes:lines.extend(['## 审核项说明','',*notes,''])
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
        from storycg import extra_part
        if extra_part.is_positive(audit):lines.append(extra_part.known_issue(int(number),audit))
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


def recover_failed_diagnoses(cycle,findings=None):
    recovered=False;findings=findings or repair_findings
    for slot in cycle['shots'].values():
        if slot.get('stop')=='diagnosis_rejected' and slot.get('validation_policy_revision',0)<DIAGNOSIS_POLICY_REVISION and actionable_issues(findings(slot['best_audit'])):
            slot.setdefault('rejected_decisions',[]).append({'stop':slot['stop'],'error':slot.get('diagnosis_error')})
            slot.pop('stop',None);slot.pop('diagnosis_error',None)
            slot['diagnosis_validation_resume']=True;slot['validation_policy_revision']=DIAGNOSIS_POLICY_REVISION;recovered=True
    if recovered:cycle['phase']='repairing'
    return recovered


def repair_schedule(cycle):
    """Resume prepared work first, then give every eligible shot one round per pass."""
    while True:
        eligible=[(n,s) for n,s in cycle['shots'].items()
            if (not cycle.get('repair_scope') or int(n) in cycle['repair_scope']['shots'])
            and (not s.get('stop') or (s['history'] and s['history'][-1].get('status')!='completed'))]
        if not eligible:return
        prepared=[item for item in eligible if item[1]['history'] and item[1]['history'][-1].get('status')!='completed']
        if prepared:current=prepared
        else:
            # A different repair problem on this shot still deserves its first attempt.
            untried=[]
            for item in eligible:
                slot=item[1]
                observed={problem_key(i['key']) for i in actionable_issues(repair_findings(slot['best_audit']))}
                attempted=attempted_problems(slot)
                if observed-attempted:untried.append(item)
            pool=untried or eligible
            first=min(len(s['history']) for n,s in pool)
            current=[item for item in pool if len(item[1]['history'])==first]
        yield from sorted(current,key=lambda item:int(item[0]))
