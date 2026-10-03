"""Frozen external supplementary-review validation; serial calls, no generation."""
import argparse
import copy
import hashlib
import json
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from storycg import detect, review, review_v3
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / 'calibration/v3.1'
ITEM = 'extra_protagonist'
RULES = {'minimum_error_samples': 3, 'minimum_ok_samples': 3,
         'fail_detection_min': .75, 'false_positive_max': .2, 'visual_repeats': 2,
         'reliability_scope': 'candidate', 'report_scope': 'all_samples',
         'detection_numerator': 'fail_only', 'failure_scoring': 'exclude_and_report',
         'max_visual_calls': 30, 'generated_images': 0,
         'max_attempts_per_layer': 3, 'backoff_seconds': [30, 120],
         'request_timeout_seconds': 180, 'max_work_seconds': 5400}


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def summarize_external(samples, records):
    expected = {(sid, repeat) for sid in samples for repeat in (1, 2)}
    keys = [(row['sample'], row['repeat']) for row in records]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError('exactly two unique rounds required for every frozen sample')
    output = {}
    for scope in ('candidate', 'system'):
        selected = {sid: label for sid, label in samples.items()
                    if scope == 'system' or label['level'] == 'candidate'}
        counts = {truth: Counter() for truth in ('error', 'ok', 'not_visible')}
        sizes = Counter(label['truth'][ITEM] for label in selected.values())
        pairs = unstable = excluded = 0
        for sid, label in selected.items():
            rows = [row for row in records if row['sample'] == sid]
            good = [row for row in rows if row['execution_status'] == 'complete']
            for row in good:
                if row['mapped'] not in ('pass', 'fail', 'not_visible'):
                    raise ValueError('invalid image verdict')
                counts[label['truth'][ITEM]][row['mapped']] += 1
            excluded += 2 - len(good)
            if len(good) == 2:
                pairs += 1
                unstable += good[0]['mapped'] != good[1]['mapped']
        errors, oks = sum(counts['error'].values()), sum(counts['ok'].values())
        detection = counts['error']['fail'] / errors if errors else None
        false_positive = counts['ok']['fail'] / oks if oks else None
        status = ('unverified' if sizes['error'] < 3 or sizes['ok'] < 3 or excluded else
                  'reliable' if detection >= .75 and false_positive <= .2 else 'unreliable')
        output[scope] = {'status': status, 'error_samples': sizes['error'],
            'ok_samples': sizes['ok'], 'not_visible_samples': sizes['not_visible'],
            'counts': {truth: dict(value) for truth, value in counts.items()},
            'fail_detection_rate': detection, 'false_positive_rate': false_positive,
            'error_runs': errors, 'ok_runs': oks, 'excluded_runs': excluded,
            'coverage_complete': not excluded, 'unstable_pairs': unstable,
            'total_pairs': pairs, 'unstable_rate': unstable / pairs if pairs else None,
            'deterministic': False}
    return output


def published_item(summary):
    row = copy.deepcopy(summary['candidate'])
    rate = summary['system']['fail_detection_rate']
    rate_text = '未取得' if rate is None else f'{rate:.2%}'
    row.update(calibration_version='3.1', calibration_results='calibration/v3.1/results.json',
        reviewer_freeze='calibration/v3.1/reviewer_freeze.json',
        system_level=copy.deepcopy(summary['system']),
        note=f'可靠性状态按检测器已给出候选的样本判定；系统层检出率 {rate_text}；检测器看不到的小号人物仍会漏。')
    return row


def publish_config(baseline, summary, target):
    result = copy.deepcopy(baseline)
    result['items'][ITEM] = published_item(summary)
    save_json_atomic(target, result)


def inputs(label_path):
    labels = load(label_path)['samples']
    hashes = {str(Path(label_path).relative_to(ROOT)): sha(label_path)}
    states = {}
    for sid, label in labels.items():
        if label['level'] not in ('candidate', 'system_only'):
            raise ValueError('unknown scoring level')
        paths = [label['path'], label['portrait'], label['job'] + '/identity.json',
                 label['job'] + '/storyboard.json']
        for path in paths:
            hashes[path] = sha(ROOT / path)
        if hashes[label['path']] != label['sha256']:
            raise ValueError('sample image hash differs from supervisor label')
        board = load(ROOT / paths[3])
        matched = [entry for entry in board['shots'] if entry['number'] == label['shot_number']]
        if len(matched) != 1:
            raise ValueError('shot number must identify one state-owned entry')
        states[sid] = {'identity': load(ROOT / paths[2]), 'entry': matched[0]}
    return {'labels': labels, 'states': states, 'hashes': hashes}


def files():
    paths = sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'src/storycg').glob('*.py'))
    paths += sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'tools').glob('p10_03*.py'))
    paths += sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'tools').glob('test_p10_03*.py'))
    paths += ['config/default.json', 'config/detect.json', 'config/detection_review.json',
              'config/identity_rules.json', 'models/detect/face_yolov8s.pt',
              'models/detect/person_yolov8s-seg.pt', 'calibration/v3/results.json',
              'calibration/v3/review_log.jsonl', 'calibration/v3/reviewer_freeze.json']
    return paths


def freeze(label_path):
    CAL.mkdir(parents=True, exist_ok=True)
    if (CAL / 'reviewer_freeze.json').exists():
        raise ValueError('freeze exists; cannot replace it')
    prior = load(ROOT / 'calibration/v3/reviewer_freeze.json')
    for path in ('config/default.json', 'config/detect.json', 'config/detection_review.json',
                 'models/detect/face_yolov8s.pt', 'models/detect/person_yolov8s-seg.pt'):
        if sha(ROOT / path) != prior['files'][path]:
            raise ValueError('original configuration or detector weights changed')
    if review_v3.PROMPT not in prior['prompt_texts']['src/storycg/review_v3.py']:
        raise ValueError('supplementary prompt changed')
    if review_v3.SETTINGS != prior['v3_crop_settings']:
        raise ValueError('crop settings changed')
    baseline = CAL / 'review_reliability_v3.json'
    if baseline.exists():
        raise ValueError('baseline exists')
    shutil.copyfile(ROOT / 'config/review_reliability.json', baseline)
    registry = inputs(label_path)
    frozen = {'version': '3.1', 'at': now(), 'rules': RULES,
        'files': {path: sha(ROOT / path) for path in files()},
        'inputs': registry, 'label_path': str(Path(label_path).relative_to(ROOT)),
        'prompt_sha256': hashlib.sha256(review_v3.PROMPT.encode()).hexdigest(),
        'prompt': review_v3.PROMPT, 'v3_crop_settings': review_v3.SETTINGS,
        'detect_settings': load(ROOT / 'config/detect.json'),
        'detection_thresholds': load(ROOT / 'config/detection_review.json'),
        'baseline_sha256': sha(baseline),
        'candidate_mapping': 'hair_color=ok AND hairstyle=ok AND eye_color!=mismatch AND NOT(declared_reflection AND is_reflection)',
        'candidate_filter': 'exclude all other persons containing primary face or IoU>=0.5 with primary person',
        'empty_candidates_path': 'audit still calls vision for supplementary outfit/head facets; empty candidates aggregate to ok',
        'development_sha256': sha(CAL / 'development_results.json')}
    save_json_atomic(CAL / 'reviewer_freeze.json', frozen)
    print('frozen', frozen['at'], len(registry['labels']), 'samples; rules fixed', flush=True)


def check():
    frozen = load(CAL / 'reviewer_freeze.json')
    assert frozen['rules'] == RULES, 'scoring or budget rule changed'
    assert frozen['files'] == {path: sha(ROOT / path) for path in frozen['files']}, 'reviewer/executor changed'
    assert frozen['inputs'] == inputs(ROOT / frozen['label_path']), 'frozen input changed'
    assert frozen['prompt'] == review_v3.PROMPT
    assert frozen['v3_crop_settings'] == review_v3.SETTINGS
    assert frozen['baseline_sha256'] == sha(CAL / 'review_reliability_v3.json')
    assert frozen['development_sha256'] == sha(CAL / 'development_results.json')
    return frozen


def is_transport(result, raw=None):
    if result['status'] == 'pass':
        return False
    error = result.get('error', '')
    if 'budget exhausted' in error:
        return False
    if (
        any(word in error for word in ('Timeout', 'ConnectError', 'ConnectionError', 'NetworkError',
            'ReadError', 'WriteError', 'RemoteProtocolError', 'LocalProtocolError')) or
        'HTTPStatusError' in error and re.search(r'\b5\d\d\b', error)):
        return True
    if isinstance(raw, str):
        if not raw.strip():
            return True
        try:
            envelope = json.loads(raw)
            if isinstance(envelope, dict) and 'choices' in envelope:
                choices = envelope['choices']
                if not choices:
                    return True
                content = choices[0].get('message', {}).get('content')
                if content is None or isinstance(content, str) and not content.strip():
                    return True
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            pass
    return False


def events():
    path = CAL / 'review_log.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def render(result):
    percent = lambda value: '—' if value is None else f'{value:.2%}'
    lines = ['# v3.1 外部验证', '',
        '规则、代码、提示词、检测阈值和执行脚本已在首次外部请求前冻结。每样本两轮，检出只计 fail。',
        '状态仅按 candidate 层：error/ok 各 ≥3 个不同样本，检出 ≥75%，误报 ≤20%。',
        '开发集事后复算单独保存，0 次调用，不参与发布状态。传输/结构失败单列，不冒充图像判定。', '',
        '| 层 | error/ok 不同样本 | 检出 | 误报 | 不一致 | 状态 |',
        '|---|---|---|---|---|---|']
    for scope, row in result['summary'].items():
        lines.append(f"| {scope} | {row['error_samples']}/{row['ok_samples']} | {row['counts']['error'].get('fail',0)}/{row['error_runs']} ({percent(row['fail_detection_rate'])}) | {row['counts']['ok'].get('fail',0)}/{row['ok_runs']} ({percent(row['false_positive_rate'])}) | {row['unstable_pairs']}/{row['total_pairs']} | {row['status']} |")
    lines += ['', published_item(result['summary'])['note'], '',
        '| 样本 | 层 | 真值 | 候选数 | 第一次 | 第二次 |', '|---|---|---|---|---|---|']
    for sid, label in result['samples'].items():
        rows = sorted((row for row in result['records'] if row['sample'] == sid), key=lambda row: row['repeat'])
        values = [f"{row['mapped']} ({row.get('observed_status')})" if row['execution_status']=='complete' else row['execution_status'] for row in rows]
        lines.append(f"| {sid} | {label['level']} | {label['truth'][ITEM]} | {len(rows[0]['prepared']['candidates'])} | {values[0]} | {values[1]} |")
    lines += ['', '用量：' + json.dumps(result['usage'], ensure_ascii=False), '',
        '空候选仍直接执行 audit：现有 API 同时审核服装与头部项；没有新增空候选短路规则。',
        '本工单未评估其他审核项，未以其响应更新可靠性。图片副本和裁切由既有像素得到，出图 0。']
    (CAL / 'summary.md').write_text('\n'.join(lines) + '\n')


def manifest():
    paths = [path for path in sorted(CAL.rglob('*')) if path.is_file()
             and path.name != 'manifest.json']
    save_json_atomic(CAL / 'manifest.json', {'generated_images': 0,
        'files': {str(path.relative_to(CAL)): sha(path) for path in paths},
        'note': 'Image artifacts are immutable source copies and pixel crops; no generation.'})


def run():
    frozen = check()
    if (CAL / 'started.json').exists() or (CAL / 'results.json').exists():
        raise ValueError('external batch already started; no rerun')
    started = time.monotonic()
    work = load(ROOT / 'reports/assets/P10-03/initial_hashes.json')
    elapsed_preparation = (datetime.now(timezone.utc) - datetime.fromisoformat(work['at'])).total_seconds()
    remaining = RULES['max_work_seconds'] - elapsed_preparation
    if remaining <= 0:
        raise ValueError('work budget exhausted')
    config = load(ROOT / 'config/default.json')
    config['review_reliability_file'] = str(CAL / 'review_reliability_v3.json')
    vlm = review.VisionClient(CAL, config['model_service_url'], max_calls=30, max_seconds=remaining)
    result = {'version': '3.1', 'complete': False, 'samples': frozen['inputs']['labels'],
        'records': [], 'attempts': [], 'detector_checks': [], 'freeze_sha256': sha(CAL / 'reviewer_freeze.json')}
    save_json_atomic(CAL / 'started.json', {'at': now(), 'freeze_sha256': result['freeze_sha256'], 'remaining_seconds': remaining})
    def persist():
        result['usage'] = {'generated_images': 0, 'review_calls': vlm.calls,
            'vision_seconds': vlm.call_seconds, 'execution_seconds': time.monotonic() - started,
            'work_seconds_since_recorded_start': elapsed_preparation + time.monotonic() - started}
        save_json_atomic(CAL / 'results.json', result)
    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if time.monotonic() - vlm.started >= remaining:
                raise RuntimeError('work time exhausted during backoff')
            time.sleep(min(30, end - time.monotonic()))
            print('backoff remaining', round(max(0, end-time.monotonic())), flush=True)
    persist()
    try:
        vlm.discover()
        consecutive_failed = 0
        for sid, label in result['samples'].items():
            state = frozen['inputs']['states'][sid]
            dest = CAL / 'artifacts' / sid
            dest.mkdir(parents=True, exist_ok=True)
            current, reference = dest / 'current.png', dest / 'reference.png'
            shutil.copyfile(ROOT / label['path'], current)
            shutil.copyfile(ROOT / label['portrait'], reference)
            for repeat in (1, 2):
                check()
                detected = detect.detect_image(current)
                reference_detected = detect.detect_image(reference)
                prepared = review_v3.prepare(current, reference, state['identity'], state['entry'],
                    dest / f'run{repeat}', result=detected, reference_result=reference_detected)
                serial_prepared = copy.deepcopy({key: value for key, value in prepared.items() if key != 'paths'})
                serial_prepared['paths'] = [str(path) for path in prepared['paths']]
                row = {'sample': sid, 'repeat': repeat, 'prepared': serial_prepared,
                    'detection': detect.json_result(detected), 'reference_detection': detect.json_result(reference_detected),
                    'mapped': None, 'execution_status': 'pending', 'attempt_call_ids': []}
                result['records'].append(row)
                persist()
                for attempt in range(1, 4):
                    if vlm.calls >= 30 or time.monotonic()-vlm.started >= remaining:
                        raise RuntimeError('visual/work budget exhausted; no additional request')
                    if attempt > 1:
                        seconds = RULES['backoff_seconds'][attempt-2]
                        result['attempts'].append({'event': 'backoff', 'sample': sid, 'repeat': repeat,
                            'before_attempt': attempt, 'seconds': seconds, 'at': now()})
                        persist(); wait(seconds)
                    audit = review_v3.audit(vlm, prepared, config, f'{sid}_run{repeat}_v3')
                    row['attempt_call_ids'].append(audit['call_id'])
                    result['attempts'].append({'event': 'attempt', 'sample': sid, 'repeat': repeat,
                        'attempt': attempt, 'at': now(), 'result': audit})
                    row['v3'] = audit
                    if audit['status'] == 'pass':
                        row.update(mapped=audit['data'][ITEM]['status'],
                            observed_status=audit['data'][ITEM]['observed_status'], execution_status='complete')
                        consecutive_failed = 0
                        persist(); break
                    response_event = next(event for event in reversed(events())
                        if event['event'] == 'response' and event['call_id'] == audit['call_id'])
                    if not is_transport(audit, response_event.get('raw_response')):
                        row['execution_status'] = 'review_failed'
                        consecutive_failed = 0
                        persist(); break
                    row['execution_status'] = 'transport_failed'
                    persist()
                if row['execution_status'] == 'transport_failed':
                    consecutive_failed += 1
                    if consecutive_failed >= 5:
                        raise RuntimeError('five consecutive exhausted transport layers')
                print(sid, repeat, 'candidates', len(prepared['candidates']), row['execution_status'], row['mapped'], 'calls', vlm.calls, flush=True)
            first, second = result['records'][-2:]
            def geometry(row):
                return {kind: row['detection'][kind] for kind in ('size', 'faces', 'persons')}
            result['detector_checks'].append({'sample': sid, 'identical': geometry(first) == geometry(second)})
            persist()
        check()
        result['summary'] = summarize_external(result['samples'], result['records'])
        result.update(complete=True, ended_at=now())
        persist(); render(result)
        publish_config(load(CAL / 'review_reliability_v3.json'), result['summary'], ROOT / 'config/review_reliability.json')
        manifest()
        verify()
    except BaseException as exc:
        result.update(error=f'{type(exc).__name__}: {exc}', ended_at=now())
        persist(); raise
    finally:
        vlm.close()


def verify():
    frozen = check()
    result = load(CAL / 'results.json')
    assert result['complete']
    assert result['samples'] == frozen['inputs']['labels']
    assert result['freeze_sha256'] == sha(CAL / 'reviewer_freeze.json')
    assert result['summary'] == summarize_external(result['samples'], result['records'])
    requests = {event['call_id']: event for event in events() if event['event'] == 'request'}
    responses = {event['call_id']: event for event in events() if event['event'] == 'response'}
    assert requests.keys() == responses.keys()
    assert len(requests) == result['usage']['review_calls'] <= 30
    assert all(event['at'] > frozen['at'] for event in requests.values())
    seen = set()
    for row in result['records']:
        label = result['samples'][row['sample']]
        paths = [Path(path) for path in row['prepared']['paths']]
        assert sha(paths[0]) == frozen['inputs']['hashes'][label['portrait']]
        assert sha(paths[1]) == label['sha256']
        expected = review_v3.extra_candidates(row['detection'], frozen['inputs']['states'][row['sample']]['entry']['shot'])
        assert [{key: value for key, value in candidate.items() if key != 'key'} for candidate in row['prepared']['candidates']] == expected
        for crop in row['prepared']['crops'].values():
            if crop:
                assert sha(crop['path']) == crop['sha256']
        for cid in row['attempt_call_ids']:
            assert cid not in seen
            seen.add(cid)
            assert requests[cid]['purpose'] == f"{row['sample']}_run{row['repeat']}_v3"
            logged_paths = [part['image_url']['url'] for message in requests[cid]['request']['messages']
                for part in message['content'] if part['type'] == 'image_url']
            assert len(logged_paths) == len(paths)
            for logged, path in zip(logged_paths, paths):
                assert (CAL / logged['path']).resolve() == path.resolve()
                assert logged['sha256'] == sha(path)
        if row['execution_status'] == 'complete':
            cid = row['v3']['call_id']
            raw = json.loads(json.loads(responses[cid]['raw_response'])['choices'][0]['message']['content'])
            Draft202012Validator(requests[cid]['request']['response_format']['json_schema']['schema']).validate(raw)
            assert raw == row['v3']['raw_data']
            text = requests[cid]['request']['messages'][0]['content'][0]['text']
            context = {key: row['prepared'][key] for key in ('index_map', 'candidates', 'reflection', 'checks', 'locked_tags', 'outfit')}
            assert text == review_v3.PROMPT + json.dumps(context, ensure_ascii=False)
            normalized = review_v3.normalize({'status': 'pass', 'data': raw}, row['prepared'],
                {'review_reliability_file': str(CAL/'review_reliability_v3.json')})
            assert normalized['data'] == row['v3']['data']
            assert normalized['validated_candidates'] == row['v3']['validated_candidates']
            assert row['mapped'] == normalized['data'][ITEM]['status']
        else:
            assert row['mapped'] is None
    assert seen == requests.keys()
    assert all(row['identical'] for row in result['detector_checks'])
    baseline = load(CAL / 'review_reliability_v3.json')
    expected_config = copy.deepcopy(baseline)
    expected_config['items'][ITEM] = published_item(result['summary'])
    assert load(ROOT / 'config/review_reliability.json') == expected_config
    saved_manifest = load(CAL / 'manifest.json')
    assert saved_manifest['generated_images'] == 0
    assert all(sha(CAL / path) == digest for path, digest in saved_manifest['files'].items())
    print('verified frozen reviewer/executor, inputs, raw responses, mappings, paired scopes and one-item publication;', len(requests), 'calls')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['freeze', 'check', 'run', 'verify'])
    parser.add_argument('--labels', type=Path, default=ROOT/'verify/P10-03-labels.json')
    args = parser.parse_args()
    if args.command == 'freeze':
        freeze(args.labels.resolve())
    elif args.command == 'check':
        frozen = check(); print('freeze checked', frozen['at'])
    else:
        globals()[args.command]()
