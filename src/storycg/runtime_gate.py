"""P9-05 orchestration outside the frozen reviewer.

The scheduling loop is derived from this repository's frozen cycle.py;
all visual audits, repair execution, budgets and candidate selection are reused.
Only detection findings routing and delivery gates differ. Raw results remain intact.
"""
from storycg.cycle import *
from storycg import cycle as frozen
from storycg.review_gate import detection_gate, persist_group_gate


def repair_findings(audit):
    results=[audit['single'],audit['consistency']]
    observations=audit.get('detection_items',{})
    statuses,notes=detection_gate(observations)
    if 'fail' in statuses:
        review=observations.get('review',{})
        # Only reliable observed failures can become a detection repair target.
        data={k:v for k,v in review.get('data',{}).items()
              if review.get('status')=='pass' and v.get('status') in ('fail','duplicate')
              and frozen.review.load_reliability({}).get(k,{}).get('status')=='reliable'}
        if data:results.append({'status':'pass','data':{k:{**v,'status':'fail'} for k,v in data.items()}})
    return results



def count_diagnosis_schema(entry):
    return review.object_schema({
        'diagnosis':review.TEXT,'hypothesis':review.TEXT,'changes':review.TEXT,
        'problem':{'const':'protagonist_count'},'primitive':{'const':'instruction_edit'},
        'edit':review.object_schema({'operation':{'const':'remove_duplicate_protagonist'},
            'target':{'const':'entry.shot.action'},'description':{'const':entry['shot']['action']}}),
        'shot':{'type':'null'},'target_shot':{'const':entry['number']}})


def diagnose(root,number,audit,entry,board,identity,cycle,vlm,*,narrative=None):
    count=any(i['key']=='protagonist_count' and i['status']=='fail' for i in issues(audit))
    methods=permitted_methods('protagonist_count',cycle,number)
    if count and methods==['instruction_edit'] and not narrative:
        label=f'decision_{number}_{len(cycle["shots"][str(number)]["history"])+1}_count_schema3'
        prompt=('Reliable local detection found duplicate protagonist instances. Diagnose this observed count failure, '
            'give a causal hypothesis and use the existing remove_duplicate_protagonist instruction edit. '
            'The edit retains the protagonist matching the registered action and removes the extra instance. '
            'Do not edit any object or invent state. '+json.dumps({'issues':issues(audit),'entry':entry},ensure_ascii=False))
        result=cached_call(root,label,vlm,'cycle_llm_'+label,review.messages(prompt),count_diagnosis_schema(entry))
        if result.get('status')!='pass':raise ValueError('count diagnostic LLM failed: '+str(result.get('error')))
        Draft202012Validator(count_diagnosis_schema(entry)).validate(result['data'])
        return frozen.validate_decision(result['data'],number,audit,entry,board,identity,cycle)
    return frozen.diagnose(root,number,audit,entry,board,identity,cycle,vlm,narrative=narrative)

def deliver(root,cycle,config,client):
    state=frozen.deliver(root,cycle,config,client)
    if cycle.get('final_review'):
        gate=persist_group_gate(root,cycle['final_review'],config)
        manifest=read(root/'manifest.json');manifest['review_gate']=gate
        save_json_atomic(root/'manifest.json',manifest)
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
            if cycle.get('repair_scope') and number not in cycle['repair_scope']['shots']:continue
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
            if bad and not cycle.get('repair_scope') and not cycle.get('narrative_attempted') and not quota(2,reserve_vision=3*len(board['shots'])+7,reserve_llm=2):
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
