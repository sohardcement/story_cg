import copy
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import p10_02_resume as resume


def outcome(status='pass',error=None):
    return {'status':status,**({'error':error} if error else {})}


def test_reuses_all_successful_layers_and_only_plans_missing():
    from p10_02_calibrate import CAL,load
    source=resume.ASSETS/'original-results.json'
    r=load(source if source.exists() else CAL/'results.json');original=copy.deepcopy(r)
    plan=resume.pending_layers(r)
    first=plan[0]
    assert first[2]=='v3'
    assert len(plan)==226
    assert r==original
    reused=resume.reused_calls(r)
    assert len(reused)==62


def test_transport_classification_does_not_retry_schema_or_nonempty_invalid_json():
    assert resume.is_transport(outcome('fail','ReadTimeout: timed out'),None)
    assert resume.is_transport(outcome('fail',"HTTPStatusError: Server error '503 Service Unavailable'"),'busy')
    assert resume.is_transport(outcome('fail','JSONDecodeError: empty'),'   ')
    assert not resume.is_transport(outcome('fail','ValidationError: bad status'),'{"status":"wrong"}')
    assert not resume.is_transport(outcome('fail','JSONDecodeError: bad json'),'{broken')
    assert not resume.is_transport(outcome('fail','review budget exhausted'),None)


def test_retry_two_times_then_transport_failed_without_losing_attempts():
    calls=[];health=[];waits=[];saved=[]
    def call():
        calls.append(1);return outcome('fail','ReadTimeout: timed out')
    controller=resume.RetryController(health=lambda:health.append(1) or True,
        wait=waits.append,on_attempt=lambda r:saved.append(copy.deepcopy(r)))
    result=controller.run(call,lambda r:None)
    assert len(calls)==3 and len(saved)==3
    assert waits==[30,120,300]
    assert result[1]=='transport_failed'
    assert controller.consecutive_failed_layers==1


def test_schema_failure_once_and_success_does_not_repeat():
    calls=[]
    c=resume.RetryController(health=lambda:True,wait=lambda s:None,on_attempt=lambda r:None)
    def bad():calls.append(1);return outcome('fail','ValidationError: invalid schema')
    result=c.run(bad,lambda r:'nonempty')
    assert len(calls)==1 and result[1]=='review_failed'
    calls.clear()
    def good():calls.append(1);return outcome()
    assert c.run(good,lambda r:'valid')[1]=='complete'
    assert len(calls)==1


def test_five_terminal_transport_layers_stop_and_success_resets_counter():
    import pytest
    c=resume.RetryController(health=lambda:True,wait=lambda s:None,on_attempt=lambda r:None)
    fail=lambda:outcome('fail','ConnectError: cannot connect')
    for _ in range(4):c.run(fail,lambda r:None)
    with pytest.raises(resume.ResumeStopped):c.run(fail,lambda r:None)


def test_health_unreachable_stop_after_thirty_minutes():
    import pytest
    now=[0.]
    clock=lambda:now[0]
    c=resume.RetryController(health=lambda:False,wait=lambda s:None,on_attempt=lambda r:None,clock=clock)
    assert not c.check_health()
    now[0]=1800.
    with pytest.raises(resume.ResumeStopped):c.check_health()


def test_scoring_view_keeps_old_regression_when_only_zoom_transport_fails():
    samples={'a':{'cohort':'v1','truth':{'eye_color':'error','outfit':'ok'},
                 'v3_truth':{'accessories_zoom':'error'},'supplement':True}}
    records=[{'sample':'a','repeat':repeat,'execution_status':'transport_failed' if repeat==1 else 'complete',
              'single':outcome(),'consistency':outcome(),'mapped':{},
              'v3':outcome('fail','ReadTimeout: timed out') if repeat==1 else outcome(),'v3_mapped':{'accessories_zoom':'pass'}}
             for repeat in (1,2)]
    result={'samples':samples,'records':records};original=copy.deepcopy(result)
    view,excluded=resume.scoring_view(result)
    assert view['samples']['a']['truth']['eye_color']=='error'
    assert view['samples']['a']['truth']['outfit']=='ok'
    assert view['samples']['a']['v3_truth']=={}
    assert excluded[0]['items']==['accessories_zoom']
    assert result==original


def test_original_failed_attempt_counts_toward_three_attempt_limit():
    calls=[];waits=[]
    c=resume.RetryController(health=lambda:True,wait=waits.append,on_attempt=lambda r:None)
    def call():calls.append(1);return outcome('fail','ReadTimeout: timed out')
    assert c.run(call,lambda r:None,[outcome('fail','ReadTimeout: timed out')])[1]=='transport_failed'
    assert len(calls)==2
    assert waits==[30,120,300]


def test_empty_completion_content_is_transport_but_invalid_nonempty_output_is_not():
    import json
    bad=outcome('fail','JSONDecodeError: invalid content')
    assert resume.is_transport(bad,json.dumps({'choices':[{'message':{'content':''}}]}))
    assert resume.is_transport(bad,json.dumps({'choices':[]}))
    assert resume.is_transport(bad,json.dumps({'choices':[{'message':{'content':None}}]}))
    assert not resume.is_transport(bad,json.dumps({'choices':[{'message':{'content':'{broken'}}]}))


def test_abandoned_request_ends_history_without_becoming_a_response(tmp_path,monkeypatch):
    import json
    monkeypatch.setattr(resume,'CAL',tmp_path)
    request={'event':'request','call_id':'session:1','at':'2026-01-01T00:00:00+00:00'}
    abandoned={'event':'request_abandoned','call_id':'session:1','request_at':request['at'],
               'sample':'cohort:sample','repeat':2,'layer':'single','reason':'host shutdown; no response received'}
    (tmp_path/'review_log.jsonl').write_text('\n'.join(json.dumps(e) for e in (request,abandoned))+'\n')
    events,requests,responses=resume.history()
    assert set(requests)==resume.ended_call_ids(events,responses)=={'session:1'}
    assert responses=={}
    attempt=resume.abandoned_outcome(abandoned)
    assert attempt['call_id']=='session:1' and attempt['status']=='fail'
    assert resume.is_transport(attempt,None)
    assert attempt['elapsed_seconds']==0 and attempt['elapsed_unknown'] is True


def test_abandonment_is_appended_once_and_uses_one_attempt(tmp_path,monkeypatch):
    import json
    import pytest
    monkeypatch.setattr(resume,'CAL',tmp_path)
    monkeypatch.setattr(resume,'validate_addendum',lambda:(None,None))
    request={'event':'request','call_id':'session:1','at':'2026-01-01T00:00:00+00:00',
             'purpose':'cohort_sample_run2_single_single_resume'}
    prefix=json.dumps(request)+'\n';(tmp_path/'review_log.jsonl').write_text(prefix)
    (tmp_path/'results.json').write_text(json.dumps({'samples':{'cohort:sample':{}},'records':[]}))
    event=resume.abandon_request('session:1','host shutdown; no response received')
    assert (event['sample'],event['repeat'],event['layer'])==('cohort:sample',2,'single')
    assert (tmp_path/'review_log.jsonl').read_text().startswith(prefix)
    with pytest.raises(ValueError):resume.abandon_request('session:1','same reason')
    calls=[];waits=[]
    c=resume.RetryController(health=lambda:True,wait=waits.append,on_attempt=lambda r:None)
    def fail():calls.append(1);return outcome('fail','ReadTimeout: timed out')
    assert c.run(fail,lambda r:None,[resume.abandoned_outcome(event)])[1]=='transport_failed'
    assert len(calls)==2 and waits==[30,120,300]


def test_each_session_budget_uses_monotonic_elapsed_and_restarts():
    assert resume.session_remaining(100,clock=lambda:100)==9000
    assert resume.session_remaining(100,clock=lambda:1900)==7200
    assert resume.session_remaining(100,clock=lambda:10000)==0
    assert resume.session_remaining(100000,clock=lambda:100000)==9000


def test_abandoned_attempt_is_saved_when_shutdown_preceded_row_persistence(tmp_path,monkeypatch):
    import json
    monkeypatch.setattr(resume,'CAL',tmp_path)
    prefix='';(tmp_path/'review_log.jsonl').write_text(prefix)
    successful={'sample':'cohort:sample','repeat':1,'single':outcome()}
    result={'records':[copy.deepcopy(successful)],'resume':{'layer_attempts':[]}}
    addendum={'retry_records':[]}
    event={'event':'request_abandoned','call_id':'session:1','request_at':'2026-01-01T00:00:00+00:00',
           'sample':'cohort:sample','repeat':2,'layer':'single','reason':'host shutdown; no response received'}
    resume.adopt_abandonments(result,addendum,[event])
    assert result['records']==[successful]
    assert len(result['resume']['layer_attempts'])==1
    assert result['resume']['layer_attempts'][0]['attempt']==1
    assert addendum['retry_records'][0]['call_id']=='session:1'
    saved=(tmp_path/'review_log.jsonl').read_text()
    resume.adopt_abandonments(result,addendum,[event])
    assert len(result['resume']['layer_attempts'])==1
    assert (tmp_path/'review_log.jsonl').read_text()==saved
    assert [json.loads(line)['event'] for line in saved.splitlines()]==['resume_retry']
    # Recovery after a later successful retry must preserve that response too.
    completed={'sample':'cohort:sample','repeat':2,'single':outcome()}
    result['records'].append(copy.deepcopy(completed))
    resume.adopt_abandonments(result,addendum,[event])
    assert result['records'][-1]==completed


def round_fixture():
    import json
    events=[];attempts=[]
    for index in range(1,4):
        cid=f'old:{index}';at=f'2026-01-01T00:{index*10:02d}:00+00:00'
        result={'status':'fail','call_id':cid,'error':'ReadTimeout: timed out','elapsed_seconds':10}
        events.extend([{'event':'request','call_id':cid,'at':at},
                       {'event':'response','call_id':cid,'status':'fail','raw_response':None},
                       {'event':'resume_health','at':at,'reachable':True}])
        attempts.append({'sample':'cohort:entry','repeat':1,'layer':'single','attempt':index,'origin':'resume','result':result})
    row={'sample':'cohort:entry','repeat':1,'single':copy.deepcopy(attempts[-1]['result']),'execution_status':'transport_failed'}
    failed={'sample':'cohort:entry','repeat':1,'layer':'single'}
    result={'records':[row],'resume':{'layer_attempts':attempts,'transport_failed':[failed],'consecutive_failed_layers':2}}
    grant={**failed,'attempt_round':2,'fault_window':{'start':'2026-01-01T00:00:00+00:00','end':'2026-01-01T01:00:00+00:00'},
           'historical_call_ids':[a['result']['call_id'] for a in attempts]}
    return result,events,grant


def test_new_round_requires_all_three_transport_attempts_inside_authorized_window():
    import pytest
    result,events,grant=round_fixture();before=copy.deepcopy(result)
    assert len(resume.qualify_retry_round(result,events,grant))==3
    assert result==before
    earlier=copy.deepcopy(events);earlier[0]['at']='2025-12-31T23:59:00+00:00'
    with pytest.raises(ValueError):resume.qualify_retry_round(result,earlier,grant)
    schema=copy.deepcopy(result);schema['resume']['layer_attempts'][0]['result']['error']='ValidationError: malformed schema'
    with pytest.raises(ValueError):resume.qualify_retry_round(schema,events,grant)


def test_new_round_activation_preserves_old_failures_and_consecutive_counter(tmp_path,monkeypatch):
    import json
    result,events,grant=round_fixture();before=copy.deepcopy(result)
    monkeypatch.setattr(resume,'CAL',tmp_path)
    (tmp_path/'review_log.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    addendum={'revisions':[{'retry_round_grants':[grant]}]}
    resume.activate_retry_rounds(result,addendum,events)
    assert result['resume']['layer_attempts']==before['resume']['layer_attempts']
    assert result['resume']['consecutive_failed_layers']==2
    assert result['resume']['retry_round_activations'][0]['previous_row']==before['records'][0]
    assert result['resume']['transport_failed']==[]
    assert resume.active_round(result,('cohort:entry',1,'single'))==2
    result['records'][0]['single']=outcome();saved=copy.deepcopy(result)
    resume.activate_retry_rounds(result,addendum,events)
    assert result==saved


def test_round_scoped_attempts_do_not_reset_ordinary_remaining_third_attempt():
    result,events,grant=round_fixture()
    result['resume']['layer_attempts'].append({'sample':'cohort:entry','repeat':1,'layer':'single',
        'attempt':1,'attempt_round':2,'origin':'resume','result':outcome()})
    assert len(resume.attempts_for_round(result,('cohort:entry',1,'single'),1))==3
    assert len(resume.attempts_for_round(result,('cohort:entry',1,'single'),2))==1
    waits=[];calls=[]
    controller=resume.RetryController(health=lambda:True,wait=waits.append,on_attempt=lambda r:None)
    controller.consecutive_failed_layers=2
    def call():calls.append(1);return outcome()
    priors=[a['result'] for a in result['resume']['layer_attempts'][:2]]
    assert controller.run(call,lambda r:None,priors)[1]=='complete'
    assert calls==[1] and waits==[120]


def test_authorized_round_first_attempt_is_a_retry_without_rewriting_old_entries():
    result,events,grant=round_fixture();old=copy.deepcopy(result['resume']['layer_attempts'][0])
    new={**old,'attempt':1,'attempt_round':2}
    record=resume.retry_record(new)
    assert record['attempt_round']==2 and record['attempt']==1
    assert 'attempt_round' not in resume.retry_record(old)
    assert result['resume']['layer_attempts'][0]==old
