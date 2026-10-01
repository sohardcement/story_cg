"""Resume only missing calibration layers using immutable v2 reviewer code."""
import copy
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator
from p9_04_calibrate import (ROOT, CAL, LABELS, check, sha, review, detection_review,
    save_json_atomic, VISUAL_ITEMS, map_visual, summarize, render)
from p9_04_execute import comparable_detection


def pending_runs(samples, records):
    completed=set()
    for row in records:
        key=(row['sample'],row['repeat'])
        if key in completed:
            raise ValueError('duplicate completed sample/repeat')
        if key[0] not in samples or key[1] not in (1,2):
            raise ValueError('unknown sample/repeat')
        completed.add(key)
    return [(sid,repeat) for sid in samples for repeat in (1,2) if (sid,repeat) not in completed]


def recover_single(response, request, entry, props, config):
    if response['status']!='pass':
        raise ValueError('cannot recover an unsuccessful raw response')
    choice=json.loads(response['raw_response'])['choices'][0]
    if choice.get('finish_reason') not in (None,'stop'):
        raise ValueError('incomplete original response')
    data=json.loads(choice['message']['content'])
    Draft202012Validator(request['request']['response_format']['json_schema']['schema']).validate(data)
    result={'status':'pass','purpose':response['purpose'],'call_id':response['call_id'],
            'elapsed_seconds':response['elapsed_seconds'],'data':data}
    review.enforce_visibility(result)
    if 'framing_observation' in result['data']:
        observed=result['data'].pop('framing_observation')
        result['framing_observation']=observed
        result['data']['framing']=review.framing_grade(entry['shot']['framing'],observed,config)
    result['prop_checklist']=review.prop_checklist(props,entry['shot'])
    return review.annotate_reliability(result,review.load_reliability(config))


def history():
    events=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
    return ({e['call_id']:e for e in events if e['event']=='request'},
            {e['call_id']:e for e in events if e['event']=='response'})


def run():
    frozen=check()
    execution=json.loads((CAL/'resume_freeze.json').read_text())
    if sha(Path(__file__))!=execution['sha256'] or sha(CAL/'reviewer_freeze.json')!=execution['reviewer_freeze_sha256']:
        raise ValueError('resume driver/reviewer freeze mismatch')
    if (CAL/'P9-04-resume-started.json').exists():
        raise ValueError('resume already started; refuse duplicate calls')
    if sha(CAL/'results.json')!=execution['checkpoint_sha256']:
        raise ValueError('checkpoint changed')
    prior=json.loads((CAL/'results.json').read_text())
    if prior['complete']:
        raise ValueError('nothing to resume')
    if prior['samples']!=frozen['samples']:
        raise ValueError('sample manifest changed')
    requests,responses=history()
    pending=pending_runs(prior['samples'],prior['records'])
    missing=set(requests)-set(responses)
    if sorted(missing)!=prior['termination']['unanswered_call_ids']:
        raise ValueError('unanswered calls differ from stopped checkpoint')
    config=json.loads((ROOT/'config/default.json').read_text())
    config['review_reliability_file']=str(CAL/'review_reliability_v1.json')
    recorded={row[layer]['call_id'] for row in prior['records'] for layer in ('single','consistency')}
    extras=set(responses)-recorded
    if sorted(extras)!=prior['termination']['responses_not_yet_in_complete_record']:
        raise ValueError('unassigned responses changed')
    result=copy.deepcopy(prior)
    result['resume']={'freeze':execution,'previous_records':len(prior['records']),
        'planned_runs':[list(p) for p in pending],'reused_single_calls':[],
        'reused_detection_samples':[], 'failure_source':'supervisor-confirmed host suspend'}
    started=time.monotonic()
    vlm=review.VisionClient(CAL,config['model_service_url'],max_calls=90,max_seconds=5400)
    save_json_atomic(CAL/'P9-04-resume-started.json',{'at':datetime.now(timezone.utc).isoformat(),
        'inhibit':'systemd-inhibit sleep:idle block','pid':os.getpid()})
    shutil.copyfile(CAL/'results.json',CAL/'P9-04-before-resume.json')
    for cid in sorted(missing):
        failure={'event':'response','session_id':requests[cid]['session_id'],'call_id':cid,
            'purpose':requests[cid]['purpose'],'raw_response':None,'elapsed_seconds':None,
            'status':'fail','error':'InterruptedByHostSuspend: request had no recorded response; local process was interrupted',
            'recorded_at':datetime.now(timezone.utc).isoformat(),
            'failure_origin':'execution_failure_accounting','source':'supervisor-confirmed suspend 2026-10-01 15:57:39 to 2026-10-02 01:20:14 Asia/Hong_Kong'}
        vlm.log(failure)
        result.setdefault('failed_calls',[]).append(failure)
    def persist():
        req,res=history()
        result['usage'].update(review_calls=len(req),received_responses=len(res),
            vision_seconds_received=sum(e['elapsed_seconds'] for e in res.values() if isinstance(e.get('elapsed_seconds'),(float,int))),
            detector_scored_runs=len(result['detector_repeat_checks']),detector_repeat_runs=len(result['detector_repeat_checks']))
        result['resume']['usage']={'generated_images':0,'review_calls':vlm.calls,
            'elapsed_seconds':time.monotonic()-started,'vision_seconds':vlm.call_seconds,
            'new_detector_scored_runs':len(result['detector_repeat_checks'])-len(prior['detector_repeat_checks']),
            'new_detector_repeat_runs':len(result['detector_repeat_checks'])-len(prior['detector_repeat_checks'])}
        save_json_atomic(CAL/'results.json',result)
    persist()
    try:
        for sid,repeat in pending:
            label=result['samples'][sid]
            sample=json.loads((ROOT/label['metadata']).read_text())
            entry=sample['shot_requirement']
            dest=CAL/'P9-04-artifacts'/sid.replace(':','_')
            existing=[r for r in result['records'] if r['sample']==sid]
            cached=[d for d in result['detector_repeat_checks'] if d['sample']==sid]
            if existing:
                context=existing[0]
                paths=[CAL/p for p in context['paths']]
                face,ref_face=context['face'],context['reference_face']
            else:
                dest.mkdir(parents=True,exist_ok=True)
                target,reference=dest/'current.png',dest/'reference.png'
                for src,out in ((ROOT/label['image'],target),(ROOT/label['reference'],reference)):
                    if out.exists():
                        if sha(src)!=sha(out):
                            raise ValueError('existing audit copy differs')
                    else:
                        shutil.copyfile(src,out)
                _,face=detection_review.detect_face_record(target,dest/'current_face.png')
                _,ref_face=detection_review.detect_face_record(reference,dest/'reference_face.png')
                paths=[reference,target]
                if face and ref_face:
                    pair=dest/'face_pair.png'
                    review.face_pair(dest/'reference_face.png',dest/'current_face.png',pair)
                    paths.append(pair)
            reference,target=paths[:2]
            if sha(target)!=label['image_sha256'] or sha(reference)!=label['reference_sha256']:
                raise ValueError('audit input hash mismatch')
            if cached:
                pair=cached[0]
                if not pair['identical'] or comparable_detection(pair['first'])!=comparable_detection(pair['repeat']):
                    raise ValueError('cached detection evidence inconsistent')
                observations=pair['first']
                if sid not in result['resume']['reused_detection_samples']:
                    result['resume']['reused_detection_samples'].append(sid)
            else:
                observations=detection_review.audit_detection(target,sample['identity'],entry,reference_path=reference,review_config=config)
                repeated=detection_review.audit_detection(target,sample['identity'],entry,reference_path=reference,review_config=config)
                identical=comparable_detection(observations)==comparable_detection(repeated)
                result['detector_repeat_checks'].append({'sample':sid,'identical':identical,
                    'bytewise_identical':observations==repeated,'ignored_metadata':['detection.timing_seconds'],
                    'first':observations,'repeat':repeated,'scored_runs':1})
                if not identical:
                    raise ValueError('new detector repeat differs')
            original_purpose=sid.replace(':','_')+f'_run{repeat}_single'
            reuse=[cid for cid in extras if requests[cid]['purpose']==original_purpose]
            if len(reuse)>1:
                raise ValueError('ambiguous original response')
            if reuse:
                cid=reuse[0]
                single=recover_single(responses[cid],requests[cid],entry,sample['props'],config)
                result['resume']['reused_single_calls'].append(cid)
                extras.remove(cid)
            else:
                single=review.audit_single(vlm,target,entry,sample['props'],config,
                    'resume_'+sid.replace(':','_')+f'_run{repeat}_single')
            consistency=review.audit_consistency(vlm,paths,review.identity_checklist(sample['identity'],entry['outfit']),config,
                'resume_'+sid.replace(':','_')+f'_run{repeat}_consistency')
            record={'sample':sid,'repeat':repeat,'single':single,'consistency':consistency,
                'detection_items':observations,'detection_review':observations['review'],
                'face':face,'reference_face':ref_face,'paths':[str(p.relative_to(CAL)) for p in paths],
                'mapped':{item:map_visual(item,single,consistency,cohort=label['cohort']) for item in VISUAL_ITEMS},
                'layer_conclusions':{'single':review.conclude(review.result_statuses(single)),
                    'consistency':review.conclude(review.result_statuses(consistency)),
                    'detection':review.conclude(review.result_statuses(observations['review']))}}
            result['records'].append(record)
            persist()
            print(sid,repeat,'resume_calls',vlm.calls,'records',len(result['records']),flush=True)
        if extras:
            raise ValueError('unused original successful responses')
        check()
        if result['records'][:len(prior['records'])]!=prior['records']:
            raise ValueError('existing completed records changed')
        result['summary']=summarize(result['samples'],result['records'])
        result['cohort_summary']={c:summarize(result['samples'],result['records'],cohort=c) for c in LABELS}
        result['v1_baseline']=json.loads((ROOT/'calibration/results.json').read_text())['summary']
        result['complete']=True
        persist()
        old=json.loads((CAL/'review_reliability_v1.json').read_text())
        save_json_atomic(ROOT/'config/review_reliability.json',{'version':2,'items':result['summary'],
            'rule':old['rule'],'freeze':frozen['files'],'calibration_results':'calibration/v2/results.json'})
        render(result)
    except BaseException as exc:
        result['resume']['error']=f'{type(exc).__name__}: {exc}'
        persist()
        raise
    finally:
        vlm.close()


if __name__=='__main__':
    run()
