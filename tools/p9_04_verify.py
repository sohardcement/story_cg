"""Read-only verification of frozen v2 files, raw responses and statistics."""
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from jsonschema import Draft202012Validator
from storycg import review
from storycg.calibration_v2 import summarize, map_visual, VISUAL_ITEMS
from p9_04_calibrate import CAL, ROOT, check, sha
from p9_04_execute import comparable_detection


def verify(*, partial=False):
    frozen=check()
    result=json.loads((CAL/'results.json').read_text())
    assert result['complete'] is (not partial)
    assert result['samples']==frozen['samples']
    records=result['records']
    counts=Counter(r['sample'] for r in records)
    if partial:
        assert set(counts)<=set(result['samples']) and all(n in (1,2) for n in counts.values())
    else:
        assert counts==Counter({key:2 for key in result['samples']})
    events=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
    requests={e['call_id']:e for e in events if e['event']=='request'}
    responses={e['call_id']:e for e in events if e['event']=='response'}
    if partial:
        assert len(requests)==result['usage']['review_calls']==result['termination']['review_requests']
        assert len(responses)==result['termination']['review_responses']
        assert sorted(set(requests)-set(responses))==result['termination']['unanswered_call_ids']
    else:
        assert len(requests)==len(responses)==result['usage']['review_calls']==len(records)*2+len(result.get('failed_calls',[]))
    reliability=json.loads((CAL/'review_reliability_v1.json').read_text())['items']
    config=json.loads((ROOT/'config/default.json').read_text())
    seen=[]
    for record in records:
        label=result['samples'][record['sample']]
        assert record['repeat'] in (1,2)
        for path,expected in zip(record['paths'][:2],[label['reference_sha256'],label['image_sha256']]):
            assert sha(CAL/path)==expected
        for layer in ('single','consistency'):
            actual=record[layer]
            cid=actual['call_id'];seen.append(cid)
            response=responses[cid]
            assert response['status']==actual['status']
            assert response['error']==actual.get('error')
            request=requests[cid]['request']
            for message in request['messages']:
                for part in message['content']:
                    if part['type']=='image_url':
                        pointer=part['image_url']['url']
                        assert sha(CAL/pointer['path'])==pointer['sha256']
            if actual['status']=='pass':
                data=json.loads(json.loads(response['raw_response'])['choices'][0]['message']['content'])
                Draft202012Validator(request['response_format']['json_schema']['schema']).validate(data)
                normalized=review.enforce_visibility({'status':'pass','data':copy.deepcopy(data)})
                if 'framing_observation' in normalized['data']:
                    observed=normalized['data'].pop('framing_observation')
                    assert observed==actual['framing_observation']
                    sample=json.loads((ROOT/label['metadata']).read_text())
                    normalized['data']['framing']=review.framing_grade(sample['shot_requirement']['shot']['framing'],observed,config)
                review.annotate_reliability(normalized,reliability)
                assert normalized['data']==actual['data'],cid
        mapped={item:map_visual(item,record['single'],record['consistency'],cohort=label['cohort']) for item in VISUAL_ITEMS}
        assert mapped==record['mapped']
    assert len(seen)==len(set(seen))
    if partial:
        extras=set(responses)-set(seen)
        assert sorted(extras)==result['termination']['responses_not_yet_in_complete_record']
        for cid in extras:
            data=json.loads(json.loads(responses[cid]['raw_response'])['choices'][0]['message']['content'])
            Draft202012Validator(requests[cid]['request']['response_format']['json_schema']['schema']).validate(data)
        assert len(result['detector_repeat_checks'])<=len(result['samples'])
    else:
        failures={e['call_id'] for e in result.get('failed_calls',[])}
        assert set(seen)==set(requests)-failures
        for failed in result.get('failed_calls',[]):
            assert responses[failed['call_id']]==failed
            assert failed['status']=='fail' and failed['raw_response'] is None
            assert failed['failure_origin']=='execution_failure_accounting'
        assert len(result['detector_repeat_checks'])==len(result['samples'])
    assert all(r['identical'] and comparable_detection(r['first'])==comparable_detection(r['repeat']) and r['scored_runs']==1 for r in result['detector_repeat_checks'])
    execution=json.loads((CAL/'execution_freeze.json').read_text())
    assert sha(ROOT/execution['path'])==execution['sha256']
    assert sha(CAL/'reviewer_freeze.json')==execution['reviewer_freeze_sha256']
    assert sha(CAL/'P9-04-initial-execution-failure.json')==execution['initial_failure_sha256']
    assert result['execution_freeze']==execution
    if result.get('resume'):
        import hashlib
        resume=json.loads((CAL/'resume_freeze.json').read_text())
        assert sha(ROOT/resume['path'])==resume['sha256']
        assert sha(CAL/'reviewer_freeze.json')==resume['reviewer_freeze_sha256']
        assert sha(CAL/'P9-04-before-resume.json')==resume['checkpoint_sha256']
        prior=json.loads((CAL/'P9-04-before-resume.json').read_text())
        assert result['records'][:len(prior['records'])]==prior['records']
        assert result['detector_repeat_checks'][:len(prior['detector_repeat_checks'])]==prior['detector_repeat_checks']
        lines=(CAL/'review_log.jsonl').read_bytes().splitlines(keepends=True)
        index=next(i for i,line in enumerate(lines) if json.loads(line).get('failure_origin')=='execution_failure_accounting')
        assert hashlib.sha256(b''.join(lines[:index])).hexdigest()==resume['review_log_before_sha256']
        assert result['resume']['usage']['review_calls']<=resume['max_new_review_calls']
        assert result['resume']['reused_single_calls']==prior['termination']['responses_not_yet_in_complete_record']
        if result['resume'].get('continuation'):
            from p9_04_continue import normalize_entry
            continued=json.loads((CAL/'continue_freeze.json').read_text())
            assert sha(ROOT/continued['path'])==continued['sha256']
            assert sha(CAL/'reviewer_freeze.json')==continued['reviewer_freeze_sha256']
            assert sha(CAL/'P9-04-before-continue.json')==continued['checkpoint_sha256']
            prior2=json.loads((CAL/'P9-04-before-continue.json').read_text())
            assert result['records'][:len(prior2['records'])]==prior2['records']
            assert result['detector_repeat_checks'][:len(prior2['detector_repeat_checks'])]==prior2['detector_repeat_checks']
            for row in result['records']:
                if row.get('input_adapter'):
                    sample=json.loads((ROOT/result['samples'][row['sample']]['metadata']).read_text())
                    entry,adapter=normalize_entry(sample['shot_requirement'])
                    assert row['input_adapter']=={**adapter,'entry':entry}
    for item in result['detector_repeat_checks']:
        matching=[r for r in records if r['sample']==item['sample']]
        assert all(r['detection_items']==item['first'] for r in matching)
    if partial:
        v1_samples={k:v for k,v in result['samples'].items() if v['cohort']=='v1'}
        assert all(counts[k]==2 for k in v1_samples)
        v1=summarize(v1_samples,records,cohort='v1')
        assert v1==json.loads((ROOT/'reports/assets/P9-04-v1-cohort.json').read_text())
        assert sha(ROOT/'config/review_reliability.json')==frozen['v1_reliability_sha256']
        return {'complete':False,'freeze_matches':True,'confirmed_samples':len(result['samples']),
            'complete_samples':sum(n==2 for n in counts.values()),'partial_samples':sum(n==1 for n in counts.values()),
            'unstarted_samples':len(result['samples'])-len(counts),'raw_responses_matched':len(seen),
            'extra_response_schema_validated':len(extras),'unanswered_requests':len(set(requests)-set(responses)),
            'v1_cohort_statistics_reproduced':True,'detector_repeat_checks':len(result['detector_repeat_checks']),
            'v1_reliability_preserved':True,'review_calls':len(requests)}
    assert summarize(result['samples'],records)==result['summary']
    assert {c:summarize(result['samples'],records,cohort=c) for c in ('v1','v2')}==result['cohort_summary']
    current=json.loads((ROOT/'config/review_reliability.json').read_text())
    assert current['version']==2 and current['items']==result['summary'] and current['freeze']==frozen['files']
    runtime=review.annotate_reliability({'status':'pass','data':{k:{'status':'pass'} for k in current['items']}},current['items'])
    assert all(v['verified']==(v['reliability']=='reliable') for v in runtime['data'].values())
    return {'freeze_matches':True,'confirmed_samples':len(result['samples']),'raw_responses_matched':len(seen),
        'statistics_reproduced':True,'detector_repeat_checks':len(result['detector_repeat_checks']),
        'runtime_annotations_checked':True,'review_calls':len(requests),
        'failed_calls_recorded':len(result.get('failed_calls',[])),
        'resume_calls':result.get('resume',{}).get('usage',{}).get('review_calls'),
        'previous_records_unchanged':bool(result.get('resume'))}


if __name__=='__main__':
    print(json.dumps(verify(partial='--partial' in sys.argv),ensure_ascii=False,indent=2))
