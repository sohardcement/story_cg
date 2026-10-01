"""Execution-only recovery; all reviewer code, thresholds and mappings stay frozen.

Detector wall-clock telemetry is retained as raw evidence but excluded from the
repeat-consistency predicate. Only the zero-visual-call initial failure can resume.
"""
import copy
from pathlib import Path
from p9_04_calibrate import (ROOT, CAL, LABELS, sha, check, review, detection_review,
    json, shutil, time, datetime, timezone, save_json_atomic, VISUAL_ITEMS,
    map_visual, summarize, render)


def comparable_detection(observation):
    result = copy.deepcopy(observation)
    result['detection'].pop('timing_seconds', None)
    return result


def run():
    frozen = check()
    output = CAL/'results.json'
    execution_path = CAL/'execution_freeze.json'
    execution = json.loads(execution_path.read_text())
    if execution['sha256'] != sha(Path(__file__)):
        raise ValueError('execution harness changed')
    archived = CAL/'P9-04-initial-execution-failure.json'
    prior = json.loads(archived.read_text())
    if prior['records'] or prior['error'] != 'ValueError: deterministic detector repeated result differs':
        raise ValueError('only a zero-vision-call telemetry mismatch may be resumed')
    if (CAL/'P9-04-execution-started.json').exists():
        raise ValueError('execution already started; no reruns')
    if (CAL/'review_log.jsonl').exists():
        raise ValueError('prior visual calls exist; refuse restarting')
    if sha(archived) != execution['initial_failure_sha256']:
        raise ValueError('archived failure changed')
    save_json_atomic(CAL/'P9-04-execution-started.json', {'at': datetime.now(timezone.utc).isoformat()})
    config = json.loads((ROOT/'config/default.json').read_text())
    config['review_reliability_file'] = str(CAL/'review_reliability_v1.json')
    started = time.monotonic()
    vlm = review.VisionClient(CAL, config['model_service_url'], max_calls=260, max_seconds=6000)
    result = {'execution_freeze':execution, 'initial_execution_failure':'calibration/v2/P9-04-initial-execution-failure.json', 'version':2, 'freeze':frozen['files'], 'samples':frozen['samples'], 'records':[],
              'complete':False, 'detector_repeat_checks':[], 'excluded':{}}
    for cohort, label_path in LABELS.items():
        labels = json.loads((ROOT/label_path).read_text())
        result['excluded'][cohort] = {k:v for k,v in labels['samples'].items() if not v['confirmed']}
    try:
        for sample_id, label in frozen['samples'].items():
            sample = json.loads((ROOT/label['metadata']).read_text())
            dest = CAL/'P9-04-artifacts'/sample_id.replace(':','_')
            dest.mkdir(parents=True,exist_ok=True)
            target, reference = dest/'current.png', dest/'reference.png'
            shutil.copyfile(ROOT/label['image'],target)
            shutil.copyfile(ROOT/label['reference'],reference)
            entry = sample['shot_requirement']
            checks = review.identity_checklist(sample['identity'], entry['outfit'])
            paths = [reference,target]
            _, face = detection_review.detect_face_record(target, dest/'current_face.png')
            _, ref_face = detection_review.detect_face_record(reference, dest/'reference_face.png')
            if face and ref_face:
                pair = dest/'face_pair.png'
                review.face_pair(dest/'reference_face.png', dest/'current_face.png', pair)
                paths.append(pair)
            persisted = [r for r in prior['detector_repeat_checks'] if r['sample']==sample_id]
            if persisted:
                observations, repeated = persisted[0]['first'], persisted[0]['repeat']
            else:
                observations = detection_review.audit_detection(target,sample['identity'],entry,
                    reference_path=reference, review_config=config)
                repeated = detection_review.audit_detection(target,sample['identity'],entry,
                    reference_path=reference, review_config=config)
            same = comparable_detection(observations) == comparable_detection(repeated)
            result['detector_repeat_checks'].append({'sample':sample_id,'identical':same,
                'bytewise_identical':observations==repeated,
                'ignored_metadata':['detection.timing_seconds'],
                'first':observations, 'repeat':repeated, 'scored_runs':1})
            if not same:
                raise ValueError('deterministic detector repeated result differs')
            for repeat in (1,2):
                purpose = sample_id.replace(':','_')+f'_run{repeat}'
                single = review.audit_single(vlm,target,entry,sample['props'],config,purpose+'_single')
                consistency = review.audit_consistency(vlm,paths,checks,config,purpose+'_consistency')
                record = {'sample':sample_id,'repeat':repeat,'single':single,'consistency':consistency,
                    'detection_items':observations,'detection_review':observations['review'],
                    'face':face,'reference_face':ref_face, 'paths':[str(p.relative_to(CAL)) for p in paths],
                    'mapped':{item:map_visual(item,single,consistency,cohort=label['cohort']) for item in VISUAL_ITEMS},
                    'layer_conclusions':{'single':review.conclude(review.result_statuses(single)),
                        'consistency':review.conclude(review.result_statuses(consistency)),
                        'detection':review.conclude(review.result_statuses(observations['review']))}}
                result['records'].append(record)
                result['usage'] = {'generated_images':0,'review_calls':vlm.calls,'elapsed_seconds':time.monotonic()-started,
                    'vision_seconds':vlm.call_seconds,'detector_scored_runs':len(result['detector_repeat_checks']),
                    'detector_repeat_runs':len(result['detector_repeat_checks'])}
                save_json_atomic(output,result)
                print(purpose,record['mapped'], 'calls',vlm.calls,flush=True)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        save_json_atomic(output,result)
        raise
    finally:
        vlm.close()
    check()
    result['summary'] = summarize(result['samples'],result['records'])
    result['cohort_summary'] = {c:summarize(result['samples'],result['records'],cohort=c) for c in LABELS}
    result['v1_baseline'] = json.loads((ROOT/'calibration/results.json').read_text())['summary']
    result['complete'] = True
    save_json_atomic(output,result)
    old = json.loads((CAL/'review_reliability_v1.json').read_text())
    save_json_atomic(ROOT/'config/review_reliability.json', {'version':2,'items':result['summary'],
        'rule':old['rule'], 'freeze':frozen['files'], 'calibration_results':'calibration/v2/results.json'})
    render(result)


if __name__ == '__main__':
    run()
