"""Execution-only continuation of an immutable reviewer; no generation calls."""
import copy
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from p10_02_calibrate import (ROOT,CAL,check,load,sha,review,review_v3,detection_review,
    normalize_entry,comparable_detection,save_json_atomic,VISUAL_ITEMS,NEW_ITEMS,
    map_visual,accessory_mapping,summarize,summarize_old,publish)

ADDENDUM=CAL/'resume_addendum.json'
ASSETS=ROOT/'reports/assets/P10-02-rework1'
LAYERS=('single','consistency','v3')
BACKOFF=(30,120,300)
MAX_NEW_CALLS=267
MAX_TOTAL_CALLS=330


class ResumeStopped(RuntimeError):pass


def required_layers(label):
    return ([*LAYERS[:2]] if label['cohort']!='v3' else [])+(['v3'] if label['supplement'] else [])


def row_complete(row,label):
    return all(row.get(layer,{}).get('status')=='pass' for layer in required_layers(label))


def pending_layers(result):
    rows={(r['sample'],r['repeat']):r for r in result['records']}
    if len(rows)!=len(result['records']):raise ValueError('duplicate sample/repeat record')
    return [(sid,repeat,layer) for sid,label in result['samples'].items() for repeat in (1,2)
            for layer in required_layers(label) if rows.get((sid,repeat),{}).get(layer,{}).get('status')!='pass']


def reused_calls(result):
    return [r[layer]['call_id'] for r in result['records'] for layer in LAYERS
            if r.get(layer,{}).get('status')=='pass']


def attempts_for_round(result,owner,number):
    return [a for a in result['resume']['layer_attempts'] if
        (a['sample'],a['repeat'],a['layer'])==owner and a.get('attempt_round',1)==number]


def retry_record(attempt):
    record={k:attempt[k] for k in ('sample','repeat','layer','attempt')}
    record.update(call_id=attempt['result']['call_id'],status=attempt['result']['status'],error=attempt['result'].get('error'))
    if 'attempt_round' in attempt:record['attempt_round']=attempt['attempt_round']
    return record


def registered_grants(addendum):
    grants={}
    for revision in addendum.get('revisions',[]):
        for grant in revision.get('retry_round_grants',[]):
            owner=(grant['sample'],grant['repeat'],grant['layer'])
            if owner in grants or grant['attempt_round']!=2:raise ValueError('only one additional round per authorized layer')
            grants[owner]=grant
    return grants


def qualify_retry_round(result,events,grant):
    owner=(grant['sample'],grant['repeat'],grant['layer'])
    old=attempts_for_round(result,owner,1)
    if grant['attempt_round']!=2 or len(old)!=3 or [a['attempt'] for a in old]!=[1,2,3]:
        raise ValueError('new round requires exactly three exhausted historical attempts')
    ids=[a['result']['call_id'] for a in old]
    if ids!=grant['historical_call_ids']:raise ValueError('historical attempts differ from authorization')
    start=datetime.fromisoformat(grant['fault_window']['start']);end=datetime.fromisoformat(grant['fault_window']['end'])
    if start>end:raise ValueError('invalid authorized fault window')
    evidence=[]
    for attempt in old:
        cid=attempt['result']['call_id']
        request=next(e for e in events if e['event']=='request' and e['call_id']==cid)
        position=next(i for i,e in enumerate(events) if e.get('call_id')==cid and e['event'] in ('response','request_abandoned'))
        response=events[position]
        if not is_transport(attempt['result'],response.get('raw_response')):
            raise ValueError('reviewer output failures cannot receive an additional round')
        # Requests give a lower bound. The next dated log event bounds completion
        # without inventing wall-clock completion from monotonic elapsed time.
        upper=response.get('at') or next((e['at'] for e in events[position+1:] if e.get('at')),None)
        if upper is None or not start<=datetime.fromisoformat(request['at'])<=datetime.fromisoformat(upper)<=end:
            raise ValueError('historical attempt lies outside the authorized infrastructure window')
        evidence.append({'call_id':cid,'request_at':request['at'],'failure_upper_bound_at':upper,
                         'error':attempt['result']['error']})
    return evidence


def active_round(result,owner):
    activations=[a for a in result['resume'].get('retry_round_activations',[]) if
        (a['sample'],a['repeat'],a['layer'])==owner]
    if len(activations)>1:raise ValueError('duplicate retry round activation')
    return activations[0]['attempt_round'] if activations else 1


def activate_retry_rounds(result,addendum,events):
    for owner,grant in registered_grants(addendum).items():
        evidence=qualify_retry_round(result,events,grant)
        if active_round(result,owner)==2:continue
        row=next(r for r in result['records'] if (r['sample'],r['repeat'])==owner[:2])
        failed={'sample':owner[0],'repeat':owner[1],'layer':owner[2]}
        if row.get('execution_status')!='transport_failed' or failed not in result['resume']['transport_failed']:
            raise ValueError('only an authorized exhausted transport layer can be reactivated')
        logged=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
        prior_event=next((e for e in logged if e['event']=='retry_round_activated' and
                         (e['sample'],e['repeat'],e['layer'])==owner),None)
        at=prior_event['at'] if prior_event else datetime.now(timezone.utc).isoformat()
        activation={**failed,'attempt_round':2,'at':at,'grant':copy.deepcopy(grant),
                    'qualification_evidence':evidence,'previous_row':copy.deepcopy(row),
                    'previous_terminal_record':copy.deepcopy(failed)}
        result['resume'].setdefault('retry_round_activations',[]).append(activation)
        result['resume']['transport_failed'].remove(failed)
        row['execution_status']='running'
        event={'event':'retry_round_activated',**{k:v for k,v in activation.items() if k!='previous_row'}}
        if prior_event:assert prior_event==event
        else:append_event(event)


def is_transport(result,raw):
    if result['status']=='pass':return False
    error=result.get('error','')
    if 'budget exhausted' in error:return False
    if any(word in error for word in ('TransportAbandoned','Timeout','ConnectError','ConnectionError','NetworkError','ReadError',
            'WriteError','RemoteProtocolError','LocalProtocolError')):return True
    if 'HTTPStatusError' in error and re.search(r"\b5\d\d\b",error):return True
    if isinstance(raw,str):
        if not raw.strip():return True
        try:
            envelope=json.loads(raw)
            if isinstance(envelope,dict) and 'choices' in envelope:
                choices=envelope['choices']
                if not choices:return True
                content=choices[0].get('message',{}).get('content')
                if content is None or isinstance(content,str) and not content.strip():return True
        except (ValueError,TypeError,KeyError,IndexError,AttributeError):pass
    return False


class RetryController:
    def __init__(self,*,health,wait,on_attempt,clock=time.monotonic):
        self.health=health;self.wait=wait;self.on_attempt=on_attempt;self.clock=clock
        self.unreachable_since=None;self.consecutive_failed_layers=0

    def check_health(self):
        good=self.health()
        if good:self.unreachable_since=None
        elif self.unreachable_since is None:self.unreachable_since=self.clock()
        if self.unreachable_since is not None and self.clock()-self.unreachable_since>=1800:
            raise ResumeStopped('service unreachable continuously for 30 minutes')
        return good

    def run(self,call,raw_for,prior_failures=()):
        prior=list(prior_failures);last=prior[-1] if prior else None
        if len(prior)>2:raise ResumeStopped('layer already exhausted three attempts')
        for attempt in range(len(prior),3):
            if attempt:
                self.check_health();self.wait(BACKOFF[attempt-1])
            last=call();self.on_attempt(last)
            if last['status']=='pass':
                self.consecutive_failed_layers=0;return last,'complete'
            if not is_transport(last,raw_for(last)):
                self.consecutive_failed_layers=0;return last,'review_failed'
        self.consecutive_failed_layers+=1
        self.check_health()
        # Third backoff is a cooldown before moving on, never a fourth request.
        if self.consecutive_failed_layers>=5:raise ResumeStopped('five consecutive layers exhausted transport retries')
        self.wait(BACKOFF[2])
        return last,'transport_failed'


def scoring_view(result):
    # The canonical registry and grades stay untouched. Frozen summaries require
    # paired runs, so only an affected item-pair loses its scoring truth.
    view=copy.deepcopy(result);exclusions=[]
    single_items={'props','action'}
    consistency_items=set(VISUAL_ITEMS)-single_items
    for sid,label in view['samples'].items():
        rows=[r for r in result['records'] if r['sample']==sid]
        missing={layer for layer in required_layers(label) if any(
            r.get(layer,{}).get('status')!='pass' for r in rows)}
        if not missing:continue
        original_truth=result['samples'][sid]['truth']
        # Preserve independently observed detection truth when its visual twin fails.
        if 'eye_color' in original_truth:label['truth']['eye_color_sampled']=original_truth['eye_color']
        if 'accessories' in original_truth:label['truth']['accessory_color_sampled']=original_truth['accessories']
        removed=[]
        for item in list(label['truth']):
            affected=(item in single_items and 'single' in missing) or (item in consistency_items and 'consistency' in missing)
            if affected:removed.append(item);del label['truth'][item]
        if 'v3' in missing:
            removed.extend(label['v3_truth']);label['v3_truth']={}
        exclusions.append({'sample':sid,'layers':sorted(missing),'items':sorted(removed),
            'reason':'unavailable visual output; both repeats excluded only for affected items; canonical truth unchanged'})
    for row in view['records']:
        label=view['samples'][row['sample']]
        if label['cohort']!='v3' and 'mapped' not in row:
            row['mapped']={}
            for item in VISUAL_ITEMS:
                layer='single' if item in single_items else 'consistency'
                if row.get(layer,{}).get('status')=='pass':
                    row['mapped'][item]=map_visual(item,row.get('single',{'status':'fail'}),
                        row.get('consistency',{'status':'fail'}),cohort=label['cohort'])
        if label['supplement'] and row.get('v3',{}).get('status')=='pass' and not row.get('v3_mapped'):
            row['v3_mapped']={item:row['v3']['data'][item]['status'] for item in NEW_ITEMS}
            row['v3_mapped'].update(accessory_mapping(row['v3'],label['cohort']))
    return view,exclusions


def history():
    events=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
    requests=[e for e in events if e['event']=='request'];responses=[e for e in events if e['event']=='response']
    for rows in (requests,responses):
        ids=[r['call_id'] for r in rows]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate call_id in raw log')
    abandoned=[e for e in events if e['event']=='request_abandoned']
    ids=[e['call_id'] for e in abandoned]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate abandonment')
    request_ids={r['call_id'] for r in requests};response_ids={r['call_id'] for r in responses}
    if not set(ids)<=request_ids or set(ids)&response_ids:raise ValueError('invalid abandonment; no response may be replaced')
    for event in abandoned:
        request=next(r for r in requests if r['call_id']==event['call_id'])
        assert event['request_at']==request['at']
    return events,{r['call_id']:r for r in requests},{r['call_id']:r for r in responses}


def ended_call_ids(events,responses):
    return set(responses)|{e['call_id'] for e in events if e['event']=='request_abandoned'}


def abandoned_outcome(event):
    # No response and no measured duration: this is an execution failure only.
    return {'status':'fail','call_id':event['call_id'],'error':'TransportAbandoned: '+event['reason'],
            'elapsed_seconds':0,'elapsed_unknown':True,'request_abandoned':True}


def append_event(event):
    with (CAL/'review_log.jsonl').open('a') as handle:
        handle.write(json.dumps(event,ensure_ascii=False)+'\n');handle.flush();os.fsync(handle.fileno())


def abandon_request(call_id,reason):
    validate_addendum();events,requests,responses=history()
    if call_id not in requests or call_id in ended_call_ids(events,responses):
        raise ValueError('only an unresolved request can be explicitly abandoned')
    request=requests[call_id];result=load(CAL/'results.json');owners=[]
    for sid in result['samples']:
        for repeat in (1,2):
            for layer in LAYERS:
                prefix=sid.replace(':','_')+f'_run{repeat}_{layer}'
                if request['purpose'].startswith(prefix+'_'):owners.append((sid,repeat,layer))
    assert len(owners)==1,'request owner is ambiguous'
    sid,repeat,layer=owners[0]
    event={'event':'request_abandoned','at':datetime.now(timezone.utc).isoformat(),'call_id':call_id,
           'request_at':request['at'],'sample':sid,'repeat':repeat,'layer':layer,'reason':reason}
    append_event(event);return event


def session_remaining(started,clock=time.monotonic):
    return max(0,9000-(clock()-started))


def adopt_abandonments(result,addendum,events):
    for event in events:
        if event['event']!='request_abandoned':continue
        sid,repeat,layer=event['sample'],event['repeat'],event['layer']
        rows=[r for r in result['records'] if (r['sample'],r['repeat'])==(sid,repeat)]
        assert len(rows)<=1
        outcome=abandoned_outcome(event)
        known=[a for a in result['resume']['layer_attempts'] if a['result']['call_id']==event['call_id']]
        if not known:
            if rows:assert rows[0].get(layer,{}).get('status')!='pass'
            assert not any((a['sample'],a['repeat'],a['layer'])==(sid,repeat,layer) for a in result['resume']['layer_attempts'])
            if rows:rows[0][layer]=outcome
            result['resume']['layer_attempts'].append({'sample':sid,'repeat':repeat,'layer':layer,
                'attempt':1,'origin':'abandoned','result':outcome})
        else:assert len(known)==1 and known[0]['result']==outcome
        record={'sample':sid,'repeat':repeat,'layer':layer,'attempt':1,'call_id':event['call_id'],
                'status':'fail','error':outcome['error']}
        saved=[r for r in addendum['retry_records'] if r['call_id']==event['call_id']]
        if not saved:addendum['retry_records'].append(record)
        else:assert saved==[record]
        logged=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
        saved=[e for e in logged if e['event']=='resume_retry' and e['call_id']==event['call_id']]
        if not saved:append_event({'event':'resume_retry',**record})
        else:assert saved==[{'event':'resume_retry',**record}]


def config_for_review():
    config=load(ROOT/'config/default.json');config['review_reliability_file']=str(CAL/'review_reliability_v2.json')
    return config


def prepare():
    frozen=check()
    if ADDENDUM.exists():raise ValueError('resume addendum already exists; use run/verify')
    ASSETS.mkdir(parents=True,exist_ok=True)
    result=load(CAL/'results.json');events,requests,responses=history()
    assert not result['complete'] and result['samples']==frozen['samples']
    assert requests.keys()==responses.keys()
    reused=reused_calls(result)
    assert len(reused)==len(set(reused))
    assert set(reused)=={cid for cid,r in responses.items() if r['status']=='pass'}
    shutil.copyfile(CAL/'results.json',ASSETS/'original-results.json')
    shutil.copyfile(CAL/'review_log.jsonl',ASSETS/'original-review-log.jsonl')
    now=datetime.now(timezone.utc)
    assert now>datetime.fromisoformat(frozen['at'])
    save_json_atomic(ADDENDUM,{'created_at':now.isoformat(),'tool':'tools/p10_02_resume.py',
        'tool_sha256':sha(Path(__file__)),'reviewer_freeze_sha256':sha(CAL/'reviewer_freeze.json'),
        'startup_check':{'passed':True,'files':frozen['files'],'labels':frozen['labels'],'samples':len(frozen['samples'])},
        'frozen_files_unchanged':True,'original_checkpoint_sha256':sha(ASSETS/'original-results.json'),
        'original_log_sha256':sha(ASSETS/'original-review-log.jsonl'),'initial_calls':len(requests),
        'reused_successful_call_ids':reused,'pending_at_start':[list(t) for t in pending_layers(result)],
        'health_checks':[],'retry_records':[],'starts':[],
        'retry_policy':{'max_attempts_per_layer':3,'backoff_seconds':list(BACKOFF),
            'explanation':'30/120 seconds precede the two retries; 300 seconds is terminal cooldown, no fourth attempt',
            'original_failed_attempts_count_toward_limit':True,'request_timeout_seconds':180},
        'task_started_at':load(ASSETS/'task-start.json')['at'] if (ASSETS/'task-start.json').exists() else now.isoformat(),
        'limits':{'new_visual_calls':267,'total_visual_calls':330,'resume_seconds':9000}})
    print('prepared resume; preserved',len(reused),'successful responses; missing',len(pending_layers(result)),'layers')


def validate_addendum():
    frozen=check();addendum=load(ADDENDUM)
    expected=addendum['tool_sha256']
    for revision in addendum.get('revisions',[]):
        assert revision['tool_sha256_before']==expected and revision['pre_change_check']['passed']
        expected=revision['tool_sha256_after']
    assert sha(Path(__file__))==expected,'resume tool changed'
    assert sha(CAL/'reviewer_freeze.json')==addendum['reviewer_freeze_sha256']
    assert sha(ASSETS/'original-results.json')==addendum['original_checkpoint_sha256']
    assert sha(ASSETS/'original-review-log.jsonl')==addendum['original_log_sha256']
    prefix=(ASSETS/'original-review-log.jsonl').read_bytes()
    assert (CAL/'review_log.jsonl').read_bytes().startswith(prefix),'old log prefix changed'
    return frozen,addendum


def build_inputs(sid,label,result,config):
    sample=load(ROOT/label['metadata']) if label.get('metadata') else label['state']
    entry,adapter=normalize_entry(sample['shot_requirement']);identity=sample['identity']
    dest=CAL/'artifacts'/sid.replace(':','_');dest.mkdir(parents=True,exist_ok=True)
    target,reference=dest/'current.png',dest/'reference.png'
    for original,copy_path in ((ROOT/label['image'],target),(ROOT/label['reference'],reference)):
        if copy_path.exists():assert sha(copy_path)==sha(original)
        else:shutil.copyfile(original,copy_path)
    cached=[r for r in result['detector_repeat_checks'] if r['sample']==sid]
    if cached:
        assert len(cached)==1 and cached[0]['identical']
        assert comparable_detection(cached[0]['first'])==comparable_detection(cached[0]['repeat'])
        observation=cached[0]['first']
    else:
        observation=detection_review.audit_detection(target,identity,entry,reference_path=reference,review_config=config)
        repeated=detection_review.audit_detection(target,identity,entry,reference_path=reference,review_config=config)
        identical=comparable_detection(observation)==comparable_detection(repeated)
        result['detector_repeat_checks'].append({'sample':sid,'identical':identical,'first':observation,'repeat':repeated,
            'scored_runs':1,'ignored_metadata':['detection.timing_seconds']})
        if not identical:raise ResumeStopped('detector repeat changed')
    paths=[reference,target]
    _,face=detection_review.detect_face_record(target,dest/'current_face.png')
    _,ref_face=detection_review.detect_face_record(reference,dest/'reference_face.png')
    if face and ref_face:
        pair=dest/'face_pair.png';review.face_pair(dest/'reference_face.png',dest/'current_face.png',pair);paths.append(pair)
    prepared=review_v3.prepare(target,reference,identity,entry,dest/'v3') if label['supplement'] else None
    return sample,entry,adapter,observation,paths,prepared


def run():
    frozen,addendum=validate_addendum();result=load(CAL/'results.json')
    if result['complete']:raise ValueError('already complete; use verify')
    assert result['samples']==frozen['samples']
    events,requests,responses=history();assert requests.keys()==ended_call_ids(events,responses),'unresolved request; do not silently resubmit'
    original=load(ASSETS/'original-results.json')
    initial_calls=addendum['initial_calls']
    used=len(requests)-initial_calls
    if used>=MAX_NEW_CALLS or len(requests)>=MAX_TOTAL_CALLS:raise ResumeStopped('visual request budget exhausted')
    created=datetime.fromisoformat(addendum['created_at'])
    started=time.monotonic()
    config=config_for_review();model=next(iter(requests.values()))['request']['model']
    vlm=review.VisionClient(CAL,config['model_service_url'],model=model,
        max_calls=min(MAX_NEW_CALLS-used,MAX_TOTAL_CALLS-len(requests)),max_seconds=session_remaining(started),timeout=180)
    old_seconds=result['usage']['vision_seconds']
    old_elapsed=result['usage']['elapsed_seconds']
    prior_resume=result.setdefault('resume',{'original_partial_records':[copy.deepcopy(r) for r in original['records']
        if not row_complete(r,original['samples'][r['sample']])], 'layer_attempts':[], 'sessions':[],
        'transport_failed':[],'review_failed':[]})
    if not prior_resume['layer_attempts']:
        for row in original['records']:
            for layer in required_layers(original['samples'][row['sample']]):
                if row.get(layer,{}).get('status')=='fail':
                    prior_resume['layer_attempts'].append({'sample':row['sample'],'repeat':row['repeat'],'layer':layer,
                        'attempt':1,'origin':'original','result':copy.deepcopy(row[layer])})
    adopt_abandonments(result,addendum,events)
    activate_retry_rounds(result,addendum,events)
    session={'session_id':vlm.session_id,'started_at':datetime.now(timezone.utc).isoformat(),'initial_total_requests':len(requests),
             'budget_seconds':9000,'clock':'monotonic','started_monotonic':started,'elapsed_seconds':0,
             'initial_consecutive_failed_layers':prior_resume.get('consecutive_failed_layers',0)}
    prior_resume['sessions'].append(session);addendum['starts'].append({'at':session['started_at'],'check_passed':True,'session_id':vlm.session_id})
    def persist():
        session['elapsed_seconds']=time.monotonic()-started
        result['usage']={'generated_images':0,'review_calls':len(requests)+vlm.calls,
            'vision_seconds':old_seconds+vlm.call_seconds,'elapsed_seconds':old_elapsed+time.monotonic()-started,
            'detector_scored_runs':len(result['detector_repeat_checks']),
            'resume_review_calls':len(requests)+vlm.calls-initial_calls,
            'resume_elapsed_seconds':old_elapsed+session['elapsed_seconds']-original['usage']['elapsed_seconds'],
            'resume_wall_seconds':(datetime.now(timezone.utc)-created).total_seconds()}
        save_json_atomic(CAL/'results.json',result);save_json_atomic(ADDENDUM,addendum)
    def health():
        record={'at':datetime.now(timezone.utc).isoformat(),'endpoint':'/models'}
        try:
            response=vlm.http.get(config['model_service_url'].rstrip('/')+'/models',timeout=20)
            record.update(status_code=response.status_code,raw_response=response.text)
            response.raise_for_status();available=response.json()['data']
            good=any(m['id']==model and m.get('loaded',True) for m in available)
        except Exception as exc:record['error']=f'{type(exc).__name__}: {exc}';good=False
        record['reachable']=good;addendum['health_checks'].append(record)
        vlm.log({'event':'resume_health',**record});persist();return good
    def sleep_checked(seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if time.monotonic()-vlm.started>=vlm.max_seconds:raise ResumeStopped('resume time budget exhausted during backoff')
            time.sleep(min(30,max(0,until-time.monotonic())))
            print('backoff',round(max(0,until-time.monotonic())),'seconds remain',flush=True)
            controller.check_health()
    controller=RetryController(health=health,wait=sleep_checked,on_attempt=lambda r:None)
    controller.consecutive_failed_layers=prior_resume.get('consecutive_failed_layers',0)
    try:
        while not controller.check_health():sleep_checked(30)
        rows={(r['sample'],r['repeat']):r for r in result['records']}
        for sid,label in result['samples'].items():
            if all((sid,repeat) in rows and (row_complete(rows[sid,repeat],label) or
                   rows[sid,repeat].get('execution_status') in ('transport_failed','review_failed')) for repeat in (1,2)):continue
            sample,entry,adapter,observation,paths,prepared=build_inputs(sid,label,result,config);persist()
            target=paths[1]
            for repeat in (1,2):
                row=rows.get((sid,repeat))
                if row and (row_complete(row,label) or row.get('execution_status') in ('transport_failed','review_failed')):continue
                if row is None:
                    row={'sample':sid,'repeat':repeat,'detection_review':observation['review'],'v3_mapped':{},'input_adapter':adapter}
                    result['records'].append(row);rows[sid,repeat]=row
                row['execution_status']='running'
                for layer in required_layers(label):
                    if row.get(layer,{}).get('status')=='pass':continue
                    owner=(sid,repeat,layer);number=active_round(result,owner)
                    prior=[a['result'] for a in attempts_for_round(result,owner,number)]
                    if prior and not all(is_transport(r,history()[2].get(r['call_id'],{}).get('raw_response')) for r in prior):
                        row['execution_status']='review_failed';break
                    def raw_for(outcome):return history()[2].get(outcome.get('call_id'),{}).get('raw_response')
                    def invoke():
                        if vlm.calls>=vlm.max_calls or time.monotonic()-vlm.started>=vlm.max_seconds:
                            raise ResumeStopped('visual request/time budget exhausted before layer')
                        purpose=sid.replace(':','_')+f'_run{repeat}_'+layer
                        if layer=='single':return review.audit_single(vlm,target,entry,sample['props'],config,purpose+'_single_resume')
                        if layer=='consistency':return review.audit_consistency(vlm,paths,
                            review.identity_checklist(sample['identity'],entry['outfit']),config,purpose+'_consistency_resume')
                        return review_v3.audit(vlm,prepared,config,purpose+'_v3_resume')
                    def on_attempt(outcome):
                        attempt=len(attempts_for_round(result,owner,number))+1
                        entry_attempt={'sample':sid,'repeat':repeat,'layer':layer,'attempt':attempt,
                            'attempt_round':number,'origin':'resume','result':copy.deepcopy(outcome)}
                        prior_resume['layer_attempts'].append(entry_attempt)
                        row[layer]=outcome
                        layer_attempts=attempts_for_round(result,owner,number)
                        if outcome['status']!='pass' and not is_transport(outcome,raw_for(outcome)):
                            row['execution_status']='review_failed'
                            failed={'sample':sid,'repeat':repeat,'layer':layer}
                            if failed not in prior_resume['review_failed']:prior_resume['review_failed'].append(failed)
                        elif len(layer_attempts)==3 and outcome['status']!='pass':
                            row['execution_status']='transport_failed'
                            failed={'sample':sid,'repeat':repeat,'layer':layer}
                            if failed not in prior_resume['transport_failed']:prior_resume['transport_failed'].append(failed)
                            prior_resume['consecutive_failed_layers']=controller.consecutive_failed_layers+1
                        if row_complete(row,label):
                            if label['cohort']!='v3':
                                row['mapped']={item:map_visual(item,row['single'],row['consistency'],cohort=label['cohort']) for item in VISUAL_ITEMS}
                            if label['supplement']:
                                row['v3_mapped']={item:row['v3']['data'][item]['status'] for item in NEW_ITEMS}
                                row['v3_mapped'].update(accessory_mapping(row['v3'],label['cohort']))
                            row['execution_status']='complete'
                        if attempt>1 or number>1:
                            record=retry_record(entry_attempt)
                            addendum['retry_records'].append(record);vlm.log({'event':'resume_retry',**record})
                        persist()
                    controller.on_attempt=on_attempt
                    try:
                        outcome,execution_status=controller.run(invoke,raw_for,prior)
                    except ResumeStopped:
                        attempts=[a['result'] for a in attempts_for_round(result,owner,number)]
                        if len(attempts)==3 and all(is_transport(r,raw_for(r)) for r in attempts):
                            row[layer]=attempts[-1];row['execution_status']='transport_failed'
                            failed={'sample':sid,'repeat':repeat,'layer':layer}
                            if failed not in prior_resume['transport_failed']:prior_resume['transport_failed'].append(failed)
                        prior_resume['consecutive_failed_layers']=controller.consecutive_failed_layers
                        persist();raise
                    # Failed objects survive in layer_attempts and the original checkpoint.
                    row[layer]=outcome
                    if execution_status!='complete':
                        row['execution_status']=execution_status
                        failed={'sample':sid,'repeat':repeat,'layer':layer}
                        if failed not in prior_resume[execution_status]:prior_resume[execution_status].append(failed)
                        persist();break
                else:
                    if label['cohort']!='v3':
                        row['mapped']={item:map_visual(item,row['single'],row['consistency'],cohort=label['cohort']) for item in VISUAL_ITEMS}
                    if label['supplement']:
                        row['v3_mapped']={item:row['v3']['data'][item]['status'] for item in NEW_ITEMS}
                        row['v3_mapped'].update(accessory_mapping(row['v3'],label['cohort']))
                    row['execution_status']='complete'
                prior_resume['consecutive_failed_layers']=controller.consecutive_failed_layers
                persist();print(sid,repeat,row['execution_status'],'total calls',result['usage']['review_calls'],'v3',row['v3_mapped'],flush=True)
        check();assert len(result['records'])==len(result['samples'])*2
        view,excluded=scoring_view(result);result['score_excluded_samples']=excluded
        result['summary']=summarize(view)
        old_samples={k:v for k,v in view['samples'].items() if v['cohort'] in ('v1','v2')}
        old_records=[r for r in view['records'] if r['sample'] in old_samples]
        result['cohort_summary']={c:summarize_old(old_samples,old_records,cohort=c) for c in ('v1','v2')}
        result['v2_baseline']=load(ROOT/'calibration/v2/results.json')['summary']
        result['complete']=True;result['original_stop_error']=original.get('error');result.pop('error',None)
        session['completed_at']=datetime.now(timezone.utc).isoformat();persist()
        publish_view=copy.deepcopy(view)
        for key in ('summary','cohort_summary','v2_baseline','usage'):publish_view[key]=result[key]
        publish(publish_view);verify()
    except BaseException as exc:
        result['resume']['stop_error']=f'{type(exc).__name__}: {exc}';persist();raise
    finally:
        session['ended_at']=datetime.now(timezone.utc).isoformat()
        session['end_reason']='completed' if result['complete'] else result['resume'].get('stop_error','stopped')
        persist();vlm.close()


def verify():
    frozen,addendum=validate_addendum();result=load(CAL/'results.json')
    assert result['complete'] and result['samples']==frozen['samples']
    assert len(result['records'])==len(frozen['samples'])*2
    keys=[(r['sample'],r['repeat']) for r in result['records']]
    assert keys==[(sid,repeat) for sid in frozen['samples'] for repeat in (1,2)]
    original=load(ASSETS/'original-results.json')
    assert result['records'][:20]==original['records'][:20]
    assert result['detector_repeat_checks'][:len(original['detector_repeat_checks'])]==original['detector_repeat_checks']
    assert len(result['detector_repeat_checks'])==len(frozen['samples'])
    assert all(r['identical'] and comparable_detection(r['first'])==comparable_detection(r['repeat']) for r in result['detector_repeat_checks'])
    events,requests,responses=history();assert requests.keys()==ended_call_ids(events,responses)
    abandoned={e['call_id']:e for e in events if e['event']=='request_abandoned'}
    assert len(requests)==result['usage']['review_calls']<=MAX_TOTAL_CALLS
    assert len(requests)-addendum['initial_calls']==result['usage']['resume_review_calls']<=MAX_NEW_CALLS
    config=config_for_review();used_successes=set();assigned=set();owners={};outcomes={}
    row_by_key={(r['sample'],r['repeat']):r for r in result['records']}
    def verify_layer(record,layer,label,owner):
        cid=record['call_id'];assigned.add(cid)
        if cid in owners:assert owners[cid]==owner and outcomes[cid]==record
        else:owners[cid]=owner;outcomes[cid]=record
        if cid in abandoned:
            event=abandoned[cid]
            assert owner==(event['sample'],event['repeat'],event['layer'])
            assert record==abandoned_outcome(event) and cid not in responses
            return
        response=responses[cid]
        assert response['status']==record['status']
        if response['status']!='pass':
            assert response.get('error')==record.get('error');return
        raw=json.loads(json.loads(response['raw_response'])['choices'][0]['message']['content'])
        Draft202012Validator(requests[cid]['request']['response_format']['json_schema']['schema']).validate(raw)
        if layer=='v3':
            assert raw==record['raw_data']
            text=requests[cid]['request']['messages'][0]['content'][0]['text']
            context=json.loads(text[len(review_v3.PROMPT):]);context['crops']=record['crop_evidence']
            normalized=review_v3.normalize({'status':'pass','data':raw},context,config)
            assert normalized['data']==record['data']
            assert normalized['validated_candidates']==record['validated_candidates']
            for crop in record['crop_evidence'].values():
                if crop:assert sha(crop['path'])==crop['sha256']
        else:
            normalized={'status':'pass','data':copy.deepcopy(raw)};review.enforce_visibility(normalized)
            if 'framing_observation' in normalized['data']:
                observed=normalized['data'].pop('framing_observation')
                meta=load(ROOT/label['metadata'])
                normalized['data']['framing']=review.framing_grade(meta['shot_requirement']['shot']['framing'],observed,config)
            review.annotate_reliability(normalized,review.load_reliability(config));assert normalized['data']==record['data']
    for row in result['records']:
        label=result['samples'][row['sample']]
        if row.get('execution_status') not in ('transport_failed','review_failed'):assert row_complete(row,label)
        for layer in required_layers(label):
            if layer in row:
                verify_layer(row[layer],layer,label,(row['sample'],row['repeat'],layer))
                if row[layer]['status']=='pass':
                    assert row[layer]['call_id'] not in used_successes;used_successes.add(row[layer]['call_id'])
        if row_complete(row,label):
            if label['cohort']!='v3':assert row['mapped']=={item:map_visual(item,row['single'],row['consistency'],cohort=label['cohort']) for item in VISUAL_ITEMS}
            if label['supplement']:
                mapped={item:row['v3']['data'][item]['status'] for item in NEW_ITEMS};mapped.update(accessory_mapping(row['v3'],label['cohort']))
                assert row['v3_mapped']==mapped
    for old_row in original['records']:
        current=row_by_key[old_row['sample'],old_row['repeat']]
        for layer in LAYERS:
            if old_row.get(layer,{}).get('status')=='pass':assert old_row[layer]==current[layer]
    for revision in addendum.get('revisions',[]):
        preservation=revision.get('response_preservation_checkpoint')
        if not preservation:continue
        assert sha(ROOT/preservation['results_path'])==preservation['results_sha256']
        assert sha(ROOT/preservation['log_path'])==preservation['log_sha256']
        assert (CAL/'review_log.jsonl').read_bytes().startswith((ROOT/preservation['log_path']).read_bytes())
        snapshot=load(ROOT/preservation['results_path'])
        assert result['resume']['layer_attempts'][:len(snapshot['resume']['layer_attempts'])]==snapshot['resume']['layer_attempts']
        for old_row in snapshot['records']:
            for layer in LAYERS:
                if old_row.get(layer,{}).get('status')=='pass':assert row_by_key[old_row['sample'],old_row['repeat']][layer]==old_row[layer]
    grants=registered_grants(addendum);activations=result['resume'].get('retry_round_activations',[])
    assert {(a['sample'],a['repeat'],a['layer']) for a in activations}==set(grants)
    for activation in activations:
        owner=(activation['sample'],activation['repeat'],activation['layer'])
        assert activation['attempt_round']==2 and activation['grant']==grants[owner]
        assert activation['qualification_evidence']==qualify_retry_round(result,events,grants[owner])
        assert activation['previous_row']['execution_status']=='transport_failed'
        assert activation['previous_row'][owner[2]]==attempts_for_round(result,owner,1)[-1]['result']
        assert activation['previous_terminal_record']=={'sample':owner[0],'repeat':owner[1],'layer':owner[2]}
    assert [{k:v for k,v in e.items() if k!='event'} for e in events if e['event']=='retry_round_activated']==[
        {k:v for k,v in a.items() if k!='previous_row'} for a in activations]
    by_layer={}
    for attempt in result['resume']['layer_attempts']:
        label=result['samples'][attempt['sample']];verify_layer(attempt['result'],attempt['layer'],label,(attempt['sample'],attempt['repeat'],attempt['layer']))
        owner=(attempt['sample'],attempt['repeat'],attempt['layer']);number=attempt.get('attempt_round',1)
        assert number==1 or number==2 and owner in grants
        key=(*owner,number);by_layer.setdefault(key,[]).append(attempt)
    for attempts in by_layer.values():
        assert len(attempts)<=3
        assert [a['attempt'] for a in attempts]==list(range(1,len(attempts)+1))
        for previous in attempts[:-1]:
            record=previous['result'];assert is_transport(record,responses.get(record['call_id'],{}).get('raw_response'))
    terminal_lists={'transport_failed':[],'review_failed':[]}
    for row in result['records']:
        status=row.get('execution_status','complete');label=result['samples'][row['sample']]
        if status=='complete':
            assert row_complete(row,label)
            for layer in required_layers(label):
                owner=(row['sample'],row['repeat'],layer)
                chain=by_layer.get((*owner,active_round(result,owner)),[])
                if chain:assert chain[-1]['result']==row[layer] and chain[-1]['result']['status']=='pass'
            continue
        assert status in terminal_lists
        terminal=[layer for layer in required_layers(label) if row.get(layer,{}).get('status')=='fail']
        assert len(terminal)==1
        layer=terminal[0];owner=(row['sample'],row['repeat'],layer)
        chain=by_layer[(*owner,active_round(result,owner))]
        assert chain[-1]['result']==row[layer]
        if status=='transport_failed':
            assert len(chain)==3 and all(is_transport(a['result'],responses.get(a['result']['call_id'],{}).get('raw_response')) for a in chain)
        else:
            assert not is_transport(chain[-1]['result'],responses[chain[-1]['result']['call_id']]['raw_response'])
        terminal_lists[status].append({'sample':row['sample'],'repeat':row['repeat'],'layer':layer})
        position=required_layers(label).index(layer)
        assert all(row.get(k,{}).get('status')=='pass' for k in required_layers(label)[:position])
        assert all(k not in row for k in required_layers(label)[position+1:])
    for status,rows in terminal_lists.items():assert rows==result['resume'][status]
    expected_retries=[retry_record(a) for a in result['resume']['layer_attempts'] if a['origin']=='abandoned' or
        a['origin']=='resume' and (a['attempt']>1 or a.get('attempt_round',1)>1)]
    assert addendum['retry_records']==expected_retries
    retry_events=[{k:v for k,v in e.items() if k!='event'} for e in events if e['event']=='resume_retry']
    assert retry_events==expected_retries
    health_events=[{k:v for k,v in e.items() if k!='event'} for e in events if e['event']=='resume_health']
    assert health_events==addendum['health_checks']
    for chain in by_layer.values():
        for previous,current in zip(chain,chain[1:]):
            before=requests[previous['result']['call_id']];after=requests[current['result']['call_id']]
            elapsed=(datetime.fromisoformat(after['at'])-datetime.fromisoformat(before['at'])).total_seconds()
            assert elapsed>=previous['result']['elapsed_seconds']+BACKOFF[current['attempt']-2]-1
    original_failures=[r[layer] for r in original['records'] for layer in LAYERS if r.get(layer,{}).get('status')=='fail']
    assert [a['result'] for a in result['resume']['layer_attempts'] if a['origin']=='original']==original_failures
    if not terminal_lists['transport_failed'] and not terminal_lists['review_failed']:
        assert len(used_successes)==frozen['expected_visual_calls']
    assert assigned==requests.keys(),'unaccounted visual request'
    assert used_successes=={cid for cid,response in responses.items() if response['status']=='pass'}
    for request in requests.values():
        for message in request['request']['messages']:
            for part in message.get('content',[]):
                if isinstance(part,dict) and part.get('type')=='image_url':
                    image=part['image_url']['url'];assert sha(CAL/image['path'])==image['sha256']
    view,excluded=scoring_view(result);assert excluded==result['score_excluded_samples']
    assert result['summary']==summarize(view)
    old_samples={k:v for k,v in view['samples'].items() if v['cohort'] in ('v1','v2')}
    old_rows=[r for r in view['records'] if r['sample'] in old_samples]
    assert result['cohort_summary']=={c:summarize_old(old_samples,old_rows,cohort=c) for c in ('v1','v2')}
    assert result['v2_baseline']==load(ROOT/'calibration/v2/results.json')['summary']
    published=load(ROOT/'config/review_reliability.json');assert published['version']==3
    assert published['freeze']==frozen['files']
    assert published['calibration_results']=='calibration/v3/results.json'
    for item,row in result['summary'].items():
        expected=copy.deepcopy(row)
        if item=='accessories_zoom' and (row['fail_detection_rate'] is None or row['fail_detection_rate']<.75):
            expected['status']='unreliable';expected['decision']='D22 转 (a)：配饰继续标注未经可靠审核'
        assert published['items'][item]==expected
    verification={'complete':True,'frozen_files_unchanged':True,'total_requests':len(requests),
        'resume_requests':len(requests)-addendum['initial_calls'],'successful_responses':len(used_successes),
        'failed_responses':[{'call_id':cid,'error':r.get('error')} for cid,r in responses.items() if r['status']!='pass'],
        'abandoned_requests':list(abandoned.values()),
        'original_successes_reused':len(addendum['reused_successful_call_ids']),
        'transport_failed':result['resume']['transport_failed'],'review_failed':result['resume']['review_failed'],
        'score_excluded_samples':excluded,'deterministic_pairs':len(result['detector_repeat_checks'])}
    verification['authorized_retry_rounds']=[{'sample':a['sample'],'repeat':a['repeat'],'layer':a['layer'],
        'attempt_round':a['attempt_round'],'attempts':len(attempts_for_round(result,(a['sample'],a['repeat'],a['layer']),2)),
        'final_status':row_by_key[a['sample'],a['repeat']][a['layer']]['status']} for a in activations]
    save_json_atomic(ASSETS/'resume-verification.json',verification)
    print('verified original freeze, raw successes/failures, retries, mappings, statistics, configuration;',len(requests),'total calls')


if __name__=='__main__':
    check()
    if sys.argv[1]=='abandon':print(json.dumps(abandon_request(sys.argv[2],sys.argv[3]),ensure_ascii=False))
    else:{'prepare':prepare,'run':run,'verify':verify}[sys.argv[1]]()
