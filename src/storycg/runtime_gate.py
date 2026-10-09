"""P9-05 orchestration outside the frozen reviewer.

The scheduling loop is derived from this repository's frozen cycle.py;
Visual audits and repair primitives are reused. Legacy findings remain readable.
Only detection findings routing and delivery gates differ. Raw results remain intact.
"""
from storycg.cycle import *
from storycg import cycle as frozen
from storycg.review_gate import detection_gate, persist_group_gate
from storycg.v1_scope import person_findings, scope_context, narrative_repair_needed, render_delivery


def repair_findings(audit):
    results=[person_findings(audit['single']),person_findings(audit['consistency'])]
    observations=audit.get('detection_items',{})
    statuses,notes=detection_gate(observations)
    if 'fail' in statuses:
        review=observations.get('review',{})
        # Only reliable observed failures can become a detection repair target.
        data={k:v for k,v in review.get('data',{}).items()
              if review.get('status')=='pass' and v.get('status') in ('fail','duplicate')
              and frozen.review.load_reliability({}).get(k,{}).get('status')=='reliable'}
        if data:results.append({'status':'pass','data':{k:{**v,'status':'fail','reliability':'reliable'} for k,v in data.items()}})
    return results



def count_diagnosis_schema(entry):
    return review.object_schema({
        'diagnosis':review.TEXT,'hypothesis':review.TEXT,'changes':review.TEXT,
        'problem':{'const':'protagonist_count'},'primitive':{'const':'instruction_edit'},
        'edit':review.object_schema({'operation':{'const':'remove_duplicate_protagonist'},
            'target':{'const':'entry.shot.action'},'description':{'const':entry['shot']['action']}}),
        'shot':{'type':'null'},'target_shot':{'const':entry['number']}})


def count_reseed_decision(root,number,audit,entry,slot):
    """Deterministic first repair for a reliable duplicate count: new seed, same conditioning."""
    import secrets
    observation=slot.get('best_audit',{}).get('detection_items',{}).get('protagonist_count',{})
    observed=observation.get('protagonist_count');allowed=observation.get('allowed_count',1)
    seed=secrets.randbits(63)
    try:
        source=next(a for a in load_state(root)['attempts']
                    if a['shot_id']==f'shot{number:02d}' and a['attempt_number']==slot['best_attempt'])
        while seed==source.get('seed'):seed=secrets.randbits(63)
    except (OSError, StopIteration, ValueError):
        pass  # No persisted source attempt to differ from; any fresh seed is valid.
    return {'problem':'protagonist_count','primitive':'seed','seed':seed,
            'diagnosis':f'local count gate observed {observed} protagonist instances (allowed {allowed})',
            'hypothesis':'duplicate protagonist is seed-level sampling noise (TASK-v2 §2.2-3); '
                         'identical conditioning with a new seed is the required first repair',
            'changes':'re-roll the sampling seed only; no prompt, storyboard or scene change',
            'shot':None,'edit':None,'target_shot':number}


def diagnose(root,number,audit,entry,board,identity,cycle,vlm,*,narrative=None):
    count='protagonist_count' in preferred_problems(audit,cycle,number,narrative=bool(narrative))
    methods=permitted_methods('protagonist_count',cycle,number)
    if count and methods==['seed'] and not narrative:
        decision=count_reseed_decision(root,number,audit,entry,cycle['shots'][str(number)])
        return validate_decision(decision,number,audit,entry,board,identity,cycle)
    if count and methods==['instruction_edit'] and not narrative:
        label=f'decision_{number}_{len(cycle["shots"][str(number)]["history"])+1}_count_schema4'
        prompt=('Reliable local detection found duplicate protagonist instances. Diagnose this observed count failure, '
            'give a causal hypothesis and use the existing remove_duplicate_protagonist instruction edit. '
            'The edit retains the protagonist matching the registered action and removes the extra instance. '
            'Do not edit any object or invent state. '+json.dumps({'issues':actionable_issues(audit),'entry':entry},ensure_ascii=False))
        result=cached_call(root,label,vlm,'cycle_llm_'+label,review.messages(prompt),count_diagnosis_schema(entry))
        if result.get('status')!='pass':raise ValueError('count diagnostic LLM failed: '+str(result.get('error')))
        Draft202012Validator(count_diagnosis_schema(entry)).validate(result['data'])
        return frozen.validate_decision(result['data'],number,audit,entry,board,identity,cycle)
    return diagnose_contract(root,number,audit,entry,board,identity,cycle,vlm,narrative=narrative)


KEY_PROP_ITEM = {'type': 'object', 'required': ['prop_id', 'relation_tags', 'location_tags'],
                 'properties': {'prop_id': {'type': 'string'},
                                'relation_tags': {'type': 'array', 'items': {'type': 'string'}},
                                'location_tags': {'type': 'array', 'items': {'type': 'string'}}}}


def diagnosis_schema(cfg, *, state=None, problems=None):
    from storycg.planning import production_schema
    if problems is not None:
        if not problems:raise ValueError('no mapped actionable FAIL item')
    schema=frozen.diagnosis_schema(cfg,state=state,problems=problems)
    shot=production_schema()['properties']['shots']['items']['properties']['shot']
    # V2-13: key_props entries must carry the fields the validators read (prop_id, relation_tags, location_tags).
    shot['properties']['key_props']={'type':'array','items':KEY_PROP_ITEM}
    schema['properties']['shot']={'anyOf':[shot, {'type':'null'}]}
    return schema


def relayout_prop_guard(revised, current):
    """V2-13 (TASK-v2 §0): a relayout may delete props but never add or change them."""
    from storycg.tags import canonical_tag
    if json.dumps(revised.get('key_props', []), sort_keys=True, ensure_ascii=False) != json.dumps(
            current.get('key_props', []), sort_keys=True, ensure_ascii=False):
        raise ValueError('修图不得新增道具：key_props 与当前镜头不同，修图只能改动作、表情等（key_props 必须与当前镜头一致）')
    allowed = {canonical_tag(tag) for tag in current.get('prop_tags', [])}
    added = [tag for tag in revised.get('prop_tags', []) if canonical_tag(tag) not in allowed]
    if added:
        raise ValueError('修图不得新增道具：prop_tags 新增了 ' + ', '.join(added) + '；修图只能从当前 prop_tags 中删除')


def validate_decision(decision,number,audit,entry,board,identity,cycle,*,narrative=None):
    if decision.get('primitive')=='relayout' and decision.get('shot') is not None:
        relayout_prop_guard(decision['shot'],entry['shot'])
    if narrative:
        revised=decision.get('shot') or {}
        if (revised.get('action'),revised.get('expression'))==(entry['shot']['action'],entry['shot']['expression']):raise ValueError('narrative repair must change expression/action')
    if decision['primitive']=='instruction_edit':
        state={'identity':identity,'entry':entry,'props':board['props']}
        instruction=grounded_instruction(decision['edit'],state)
        slot=cycle['shots'][str(number)]
        for previous in slot['history']:
            old=previous['decision']
            same_source=previous.get('source_attempt')==slot['best_attempt'] or (
                previous.get('source_sha256') is not None and previous['source_sha256']==slot.get('best_audit',{}).get('image',{}).get('sha256'))
            if same_source and old['primitive']=='instruction_edit':
                prior=grounded_instruction(old['edit'],state)
                if prior==instruction:raise ValueError('cannot repeat identical instruction edit on the same source')
    return frozen.validate_decision(decision,number,audit,entry,board,identity,cycle,narrative=narrative)


def diagnose_contract(root,number,audit,entry,board,identity,cycle,vlm,*,narrative=None):
    from storycg.shot_contract import RULE_PROMPT
    cfg=cycle['config'];slot=cycle['shots'][str(number)]
    state=diagnosis_state(identity,entry,board['props'])
    audit=[person_findings(r) for r in audit]
    problems=preferred_problems(audit,cycle,number,narrative=bool(narrative))
    if not problems:raise ValueError('no mapped actionable FAIL item')
    schema=diagnosis_schema(cfg,state=state,problems=problems)
    choices={key:edit_choices(key,state) for key in problems}
    content={'issues':actionable_issues(audit),'state':state,
        'allowed_methods':{key:permitted_methods(key,cycle,number) for key in problems},
        'exact_edit_descriptions':{edit['target']:edit['description'] for values in choices.values() for edit in values},
        'allowed_edits':choices,
        'previous_repairs':[{'primitive':h['decision']['primitive'],'diagnosis':h['decision']['diagnosis'],
            'hypothesis':h['decision']['hypothesis'],'changes':h['decision']['changes'],'edit':h['decision'].get('edit'),
            'source_attempt':h['source_attempt'],'result':{k:h.get('result',{}).get(k) for k in
                ('improved','reliable_fails_before','reliable_fails_after','conclusion')}} for h in slot['history']]}
    if narrative:content.update(blind_comparison=narrative,storyboard=board)
    prompt=('Interpret actual FAIL findings. Give a diagnosis, causal hypothesis and corrective change. '
        'Choose ONLY allowed_methods for the observed problem; target_shot must equal '+str(number)+'. '
        'For instruction_edit supply exact registered target/description and shot=null. '
        'relayout: edit=null, return the complete revised shot; changes must target expression/action. '
        'Return ALL fields, including pose, action, description, tags, reflection, protagonist_instances, '
        'subject_visibility, other_people, hand_owners, and prop_tags. '
        'A relayout must keep key_props exactly as the current shot has them and may only remove entries from prop_tags; '
        'never add a prop or change a prop. '
        'action and description equal pose; tags contain only pose/expression. '
        'face_detail: edit=null and shot=null; faces are detected locally. '
        'Do not merely change seed or synonyms. '+RULE_PROMPT.replace(
            'The script derives action, description and shot.tags from pose/expression; do not return these derived fields.',
            'For relayout return action and description equal to pose and tags containing pose and expression.')+
        json.dumps(content,ensure_ascii=False))
    label=f'decision_{number}_{len(slot["history"])+1}'+('_narrative' if narrative else '')+'_contract6'
    feedback=''
    for revision in range(cfg.get('diagnosis_validation_retries',1)+1):
        name=label if revision==0 else label+f'_validation{revision}'
        result=cached_call(root,name,vlm,'cycle_llm_'+name,review.messages(prompt+feedback),schema)
        try:
            if result.get('status')!='pass':raise ValueError('diagnostic LLM failed: '+str(result.get('error')))
            Draft202012Validator(schema).validate(result['data'])
            return validate_decision(result['data'],number,audit,entry,board,identity,cycle,narrative=narrative)
        except (ValueError, __import__('jsonschema').ValidationError, KeyError, TypeError, IndexError) as exc:
            # V2-13: a malformed diagnosis (missing or wrong-typed fields) is a rejection, never a task crash.
            if isinstance(exc, (KeyError, TypeError, IndexError)):
                exc = ValueError(f'diagnosis output malformed ({type(exc).__name__}): {exc}')
            save_json_atomic(root/'cycle_artifacts'/f'{name}_rejected.json',{'error':str(exc),'original_response_file':f'{name}.json'})
            feedback='\nScript rejected previous output: '+str(exc)+'. Correct the specific field; retain registered state. Previous output: '+json.dumps(result.get('data'),ensure_ascii=False)
            if revision==cfg.get('diagnosis_validation_retries',1):raise exc

def deliver(root,cycle,config,client):
    state=frozen.deliver(root,cycle,config,client)
    if cycle.get('final_review'):
        gate=persist_group_gate(root,cycle['final_review'],config)
        manifest=read(root/'manifest.json');manifest['review_gate']=gate
        save_json_atomic(root/'manifest.json',manifest)
        render_delivery(root,cycle['final_review'],gate,config)
    return state



def reopen_targeted_repair(root,numbers,*,max_new_images=8,max_minutes=60):
    root=Path(root);cycle=read(root/'cycle.json')
    if cycle.get('phase')!='complete':raise ValueError('targeted repair requires a completed cycle')
    if not numbers or any(str(n) not in cycle['shots'] for n in numbers):raise ValueError('unknown repair shot')
    archive=root/'cycle_artifacts/pre_targeted_repair.json'
    if archive.exists():raise ValueError('targeted repair already initialized; resume existing scope')
    save_json_atomic(archive,cycle)
    baseline=usage(root)
    cycle['repair_scope']={'shots':numbers,'baseline_usage':baseline,'max_new_images':max_new_images,
        'max_minutes':max_minutes,'started_at':datetime.now(timezone.utc).isoformat()}
    cycle['config'].update(max_images=baseline['submission_attempts']+max_new_images,
        max_minutes=baseline['elapsed_seconds']/60+max_minutes,
        max_vision_calls=baseline['review_calls']+settings()['max_vision_calls'],
        max_llm_calls=baseline['llm_calls']+settings()['max_llm_calls'])
    for n in numbers:
        slot=cycle['shots'][str(n)]
        slot.setdefault('prior_stops',[]).append({'stop':slot.pop('stop',None),'error':slot.pop('diagnosis_error',None)})
        slot['validation_policy_revision']=3
    cycle['phase']='repairing';cycle.pop('finished_at',None)
    save_json_atomic(root/'cycle.json',cycle)

def run_repair_cycle(job_dir,config,client):
    root=Path(job_dir).resolve();path=root/'cycle.json'
    identity=read(root/'identity.json');board=read(root/'storyboard.json')
    cycle=read(path) if path.exists() else {'version':1,'config':settings(),'shots':{},'phase':'initial_audit',
        'allow_obscured':config.get('allow_obscured_protagonist',False)}
    if not (root/'initial_storyboard.json').exists():save_json_atomic(root/'initial_storyboard.json',board)
    cfg=cycle['config']
    recovered=recover_failed_diagnoses(cycle,repair_findings)
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
        for number_text,slot in repair_schedule(cycle):
            number=int(number_text)
            if cycle.get('repair_scope') and number not in cycle['repair_scope']['shots']:continue
            while not slot.get('stop') or (slot['history'] and slot['history'][-1].get('status')!='completed'):
                before=slot['best_audit'];results=repair_findings(before)
                pending=slot['history'][-1] if slot['history'] and slot['history'][-1].get('status')!='completed' else None
                if pending:
                    from storycg.repairs import REPAIR_PRIMITIVES
                    method=pending['decision'].get('primitive')
                    if method not in REPAIR_PRIMITIVES:
                        pending.update(status='completed',skipped=True,error='unsupported repair method: '+str(method)+' was removed in v2; no task submitted')
                        slot['stop']='unsupported_repair_method';persist();break
                if not pending:
                    actionable=actionable_issues(results)
                    if not actionable:slot['stop']='no_actionable_fail';persist();break
                    u=usage(root);reason=stop_reason(len(slot['history']),slot['stagnant'],u['submission_attempts'],u['elapsed_seconds'],cfg=cfg) or quota(reserve_vision=7+3*len(board['shots'])+2,reserve_llm=1)
                    if u['elapsed_seconds']>=(cfg['max_minutes']-cfg.get('final_review_reserve_minutes',3))*60:reason='final_review_time_reserved'
                    if reason:slot['stop']=reason;persist();break
                    try:
                        decision=diagnose(root,number,results,slot['entry'],board,identity,cycle,vlm)
                    except (ValueError, __import__('jsonschema').ValidationError) as exc:
                        slot['stop']='diagnosis_rejected';slot['diagnosis_error']=str(exc);slot['validation_policy_revision']=DIAGNOSIS_POLICY_REVISION;persist();break
                    cost=2 if decision['primitive']=='relayout' else 1
                    if usage(root)['elapsed_seconds']>=(cfg['max_minutes']-cfg.get('final_review_reserve_minutes',8))*60:
                        slot['stop']='final_review_time_reserved';persist();break
                    if quota(cost,reserve_vision=7+3*len(board['shots'])+2):slot['stop']=quota(cost,reserve_vision=7+3*len(board['shots'])+2);persist();break
                    pending={'round':len(slot['history'])+1,'decision':decision,'status':'prepared','source_attempt':slot['best_attempt'],'source_sha256':slot['best_audit']['image']['sha256']}
                    slot['history'].append(pending);persist()
                try:
                    apply_round(root,number,slot,pending,board,identity,cycle,config,client,vlm)
                except ValueError as exc:
                    if not str(exc).startswith('non_subject_mask_ambiguous'):raise
                    pending.update(status='completed',skipped=True,error=str(exc))
                    slot['stop']='non_subject_mask_ambiguous'
                persist()
                break  # Resume pending work, then share remaining rounds across shots.
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
            bad=narrative_repair_needed(narrative,scope_context(root))
            if bad and not cycle.get('repair_scope') and not cycle.get('narrative_attempted') and not quota(2,reserve_vision=3*len(board['shots'])+7,reserve_llm=2):
                # Runtime identifies the missing visible cue and target, within the same mapping.
                choose=cached_call(root,'narrative_target',vlm,'cycle_llm_narrative_target',review.messages(
                    'Identify one shot missing a PERSON expression/action cue for the key event/ending. Do not target object ownership/state/count/contact that the image-only retelling failed to read. Choose its number; do not claim it passed. '+json.dumps({'comparison':narrative,'board':board},ensure_ascii=False)),review.object_schema({'target_shot':{'type':'integer','minimum':1,'maximum':len(board['shots'])},'missing_cue':review.TEXT}))
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
                            pending={'round':len(slot['history'])+1,'decision':decision,'status':'prepared','source_attempt':slot['best_attempt'],'source_sha256':slot['best_audit']['image']['sha256'],'narrative':True}
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
        from storycg.shot_vocab import record_observations
        record_observations(root, config)
    except Exception as exc:
        cycle['blocked']=f'{type(exc).__name__}: {exc}';persist()
        if len(cycle['shots'])==len(board['shots']):deliver(root,cycle,config,client)
        raise
    finally:vlm.close()
    return deliver(root,cycle,config,client)


def apply_round(root,number,slot,pending,board,identity,cycle,config,client,vlm):
    from storycg.repairs import repair,record_result
    decision=pending['decision'];entry=slot['entry']
    allowed_decision(decision,cycle['config'])
    validate_problem(decision['problem'],repair_findings(slot['best_audit']),bool(pending.get('narrative')))
    candidate=copy.deepcopy(entry)
    if decision['shot'] is not None:candidate['shot']=decision['shot']
    diagnosis={k:decision[k] for k in ('diagnosis','hypothesis','changes')};diagnosis['result']={'status':'pending'}
    kwargs={'repair_id':f'cycle{pending["round"]}', 'source_attempt':pending['source_attempt']}
    if decision['primitive']=='instruction_edit':kwargs['edit']=decision['edit']
    if decision['primitive']=='relayout':kwargs['revised_entry']=candidate
    if decision['primitive']=='seed':kwargs['seed']=decision['seed']
    if decision['primitive']=='face_detail':
        from storycg.detection_review import repair_mask
        source=root/slot['best_audit']['image']['path']
        region=repair_mask(source,None,'face',root/'cycle_artifacts'/f'mask_{number}_{pending["round"]}')
        if region['provenance']['uncertain']:
            raise ValueError('non_subject_mask_ambiguous: merged segmentation requires experimental review')
        kwargs.update(bbox=region['bbox'],mask_path=region['mask_path'],mask_sha256=region['mask_sha256'],
            region_source=region['provenance'])
    label=f'repair_{number}_{pending["round"]}'
    before_neighbors=neighbors(root,number,slot['best_audit']['image'],entry,board,cycle,config,vlm,label+'_before')
    rec=repair(root,number,decision['primitive'],diagnosis,client,**kwargs)
    label=f'repair_{number}_{pending["round"]}'
    audited=audit_candidate(root,number,rec['output'],candidate,board,identity,config,vlm,label)
    adjacent=neighbors(root,number,rec['output'],candidate,board,cycle,config,vlm,label)
    before=[*comparison_findings(slot['best_audit']),*before_neighbors];after=[*comparison_findings(audited),*adjacent]
    better=candidate_improved(before,after) and rec.get("boundary_check",{"passed":True})["passed"]
    from storycg.cycle import reliable_regressions
    regressions=reliable_regressions([person_findings(r) for r in before],[person_findings(r) for r in after])
    if decision['problem']=='protagonist_count':
        recheck=audited.get('detection_items',{}).get('protagonist_count',{})
        better=better and recheck.get('status')=='ok'
        pending['count_recheck']=recheck
        pending['fallback_required']=recheck.get('status')=='duplicate'
    # Reliability of adjacent scene checks is unverified; retain all observations, never call them improvement.
    pending.update(status='completed',output=rec['output'],audit=audited,neighbors=adjacent,before_neighbors=before_neighbors,
        result={'reliable_fails_before':reliable_failures(before),'reliable_fails_after':reliable_failures(after),
                'reliable_failure_vector_before':list(reliable_failure_vector(before)),
                'reliable_failure_vector_after':list(reliable_failure_vector(after)),
                'failure_tiers':list(REPAIR_FAILURE_TIER_NAMES),
                'improved':better,'improvement_basis':'reliable_severity_vector','conclusion':audited['conclusion'],'unreliable_changes':issues(after),
                'reliable_regressions':regressions})
    key=f'shot{number:02d}_cycle{pending["round"]}'
    record_result(root,key,{'file':f'cycle_artifacts/{label}.json'},'Automatic audit record; human inspection pending, no human judgment inferred.')
    if better:
        state=load_state(root);a=next(a for a in state['attempts'] if a.get('repair_key')==key and a.get('repair_stage')=='final')
        slot.update(best_attempt=a['attempt_number'],best_audit=audited,entry=candidate,stagnant=0)
        board['shots'][number-1]=candidate
        save_json_atomic(root/'storyboard.json',board)
    else:slot['stagnant']+=1


def candidate_improved(before,after):
    return frozen.candidate_improved([person_findings(r) for r in before], [person_findings(r) for r in after])


def comparison_findings(audit):
    return [person_findings(r) for r in frozen.comparison_findings(audit)]


def reliable_failure_vector(results):
    return frozen.reliable_failure_vector([person_findings(r) for r in results])


def reliable_failures(results):
    return frozen.reliable_failures([person_findings(r) for r in results])
