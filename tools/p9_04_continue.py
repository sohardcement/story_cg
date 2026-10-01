"""Resume only missing calibration layers using immutable v2 reviewer code."""
import copy
import json
import hashlib
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


def normalize_entry(entry):
    result=copy.deepcopy(entry)
    adapted=[]
    for index,prop in enumerate(result['shot'].get('key_props',[])):
        if 'prop_id' in prop:
            continue
        if not isinstance(prop.get('name'),str) or not prop['name'].strip():
            raise ValueError('legacy prop without a state-supplied name')
        encoded=json.dumps(prop,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        prop['prop_id']='legacy_'+hashlib.sha256(encoded).hexdigest()[:20]
        adapted.append({'index':index,'prop_id':prop['prop_id']})
    return result, ({'operation':'legacy_named_prop_reference_id','added':adapted,
                     'original_entry_sha256':hashlib.sha256(json.dumps(entry,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
                    if adapted else None)


def run():
    frozen=check()
    execution=json.loads((CAL/'continue_freeze.json').read_text())
    if sha(Path(__file__))!=execution['sha256'] or sha(CAL/'reviewer_freeze.json')!=execution['reviewer_freeze_sha256']:
        raise ValueError('continuation driver/reviewer freeze mismatch')
    if (CAL/'P9-04-continue-started.json').exists():
        raise ValueError('continuation already started')
    if sha(CAL/'results.json')!=execution['checkpoint_sha256']:
        raise ValueError('checkpoint changed')
    prior=json.loads((CAL/'results.json').read_text())
    if prior['complete'] or prior['samples']!=frozen['samples']:
        raise ValueError('invalid continuation checkpoint')
    requests,responses=history()
    pending=pending_runs(prior['samples'],prior['records'])
    missing=set(requests)-set(responses)
    if missing:
        raise ValueError('unresolved calls before continuation')
    config=json.loads((ROOT/'config/default.json').read_text())
    config['review_reliability_file']=str(CAL/'review_reliability_v1.json')
    recorded={row[layer]['call_id'] for row in prior['records'] for layer in ('single','consistency')}
    extras={cid for cid,r in responses.items() if r['status']=='pass'}-recorded
    if extras:
        raise ValueError('unexpected unassigned successful responses')
    result=copy.deepcopy(prior)
    result['resume']['continuation']={'freeze':execution,'previous_records':len(prior['records']),
        'planned_runs':[list(p) for p in pending],'input_adapters':[],
        'reused_detection_samples':[],'previous_error':prior['resume'].get('error')}
    started=time.monotonic()
    original_started=datetime.fromisoformat(json.loads((CAL/'P9-04-resume-started.json').read_text())['at'])
    remaining=5400-(datetime.now(timezone.utc)-original_started).total_seconds()
    if remaining<=0:
        raise ValueError('continuation phase time budget exhausted')
    original_call_count=prior['resume']['usage']['review_calls']
    vlm=review.VisionClient(CAL,config['model_service_url'],max_calls=90-original_call_count,max_seconds=remaining)
    save_json_atomic(CAL/'P9-04-continue-started.json',{'at':datetime.now(timezone.utc).isoformat(),
        'inhibit':'systemd-inhibit sleep:idle block','pid':os.getpid()})
    shutil.copyfile(CAL/'results.json',CAL/'P9-04-before-continue.json')
    def persist():
        req,res=history()
        result['usage'].update(review_calls=len(req),received_responses=len(res),
            vision_seconds_received=sum(e['elapsed_seconds'] for e in res.values() if isinstance(e.get('elapsed_seconds'),(float,int))),
            detector_scored_runs=len(result['detector_repeat_checks']),detector_repeat_runs=len(result['detector_repeat_checks']))
        result['resume']['continuation']['usage']={'review_calls':vlm.calls,
            'elapsed_seconds':time.monotonic()-started,'vision_seconds':vlm.call_seconds}
        result['resume']['usage']={'generated_images':0,'review_calls':original_call_count+vlm.calls,
            'elapsed_seconds':(datetime.now(timezone.utc)-original_started).total_seconds(),
            'vision_seconds':prior['resume']['usage']['vision_seconds']+vlm.call_seconds,
            'new_detector_scored_runs':len(result['detector_repeat_checks'])-30,
            'new_detector_repeat_runs':len(result['detector_repeat_checks'])-30}
        save_json_atomic(CAL/'results.json',result)
    persist()
    try:
        for sid,repeat in pending:
            label=result['samples'][sid]
            sample=json.loads((ROOT/label['metadata']).read_text())
            entry,adapter=normalize_entry(sample['shot_requirement'])
            dest=CAL/'P9-04-artifacts'/sid.replace(':','_')
            existing=[r for r in result['records'] if r['sample']==sid]
            cached=[d for d in result['detector_repeat_checks'] if d['sample']==sid]
            if existing:
                context=existing[0]
                paths=[CAL/p for p in context['paths']]
                face,ref_face=context['face'],context['reference_face']
            elif cached:
                reference,target=dest/'reference.png',dest/'current.png'
                paths=[reference,target]
                def persisted_crop(path):
                    return {'path':str(path),'sha256':sha(path),'method':'persisted_local_face_crop'} if path.exists() else None
                face=persisted_crop(dest/'current_face.png')
                ref_face=persisted_crop(dest/'reference_face.png')
                if face and ref_face:
                    pair=dest/'face_pair.png'
                    if not pair.is_file():
                        raise ValueError('cached face pair missing')
                    paths.append(pair)
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
                if sid not in result['resume']['continuation']['reused_detection_samples']:
                    result['resume']['continuation']['reused_detection_samples'].append(sid)
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
                    'continue_'+sid.replace(':','_')+f'_run{repeat}_single')
            consistency=review.audit_consistency(vlm,paths,review.identity_checklist(sample['identity'],entry['outfit']),config,
                'continue_'+sid.replace(':','_')+f'_run{repeat}_consistency')
            record={'sample':sid,'repeat':repeat,'single':single,'consistency':consistency,
                'detection_items':observations,'detection_review':observations['review'],
                'face':face,'reference_face':ref_face,'paths':[str(p.relative_to(CAL)) for p in paths],
                'mapped':{item:map_visual(item,single,consistency,cohort=label['cohort']) for item in VISUAL_ITEMS},
                'layer_conclusions':{'single':review.conclude(review.result_statuses(single)),
                    'consistency':review.conclude(review.result_statuses(consistency)),
                    'detection':review.conclude(review.result_statuses(observations['review']))}}
            if adapter:
                record['input_adapter']={**adapter,'entry':entry}
                result['resume']['continuation']['input_adapters'].append({'sample':sid,'repeat':repeat,**adapter})
            result['records'].append(record)
            persist()
            print(sid,repeat,'resume_calls',original_call_count+vlm.calls,'records',len(result['records']),flush=True)
        if extras:
            raise ValueError('unused original successful responses')
        check()
        if result['records'][:len(prior['records'])]!=prior['records']:
            raise ValueError('existing completed records changed')
        result['summary']=summarize(result['samples'],result['records'])
        result['cohort_summary']={c:summarize(result['samples'],result['records'],cohort=c) for c in LABELS}
        result['v1_baseline']=json.loads((ROOT/'calibration/results.json').read_text())['summary']
        result['complete']=True
        result['resume']['error_history']=[prior['resume'].get('error')]
        result['resume'].pop('error',None)
        persist()
        old=json.loads((CAL/'review_reliability_v1.json').read_text())
        save_json_atomic(ROOT/'config/review_reliability.json',{'version':2,'items':result['summary'],
            'rule':old['rule'],'freeze':frozen['files'],'calibration_results':'calibration/v2/results.json'})
        render(result)
    except BaseException as exc:
        result['resume']['continuation']['error']=f'{type(exc).__name__}: {exc}'
        persist()
        raise
    finally:
        vlm.close()


if __name__=='__main__':
    run()
