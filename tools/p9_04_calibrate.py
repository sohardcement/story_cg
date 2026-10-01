"""Freeze v2 and run a single serial batch on supervisor-confirmed labels."""
import ast
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from storycg import review, detection_review
from storycg.calibration_v2 import VISUAL_ITEMS, map_visual, summarize
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / 'calibration/v2'
FILES = ['src/storycg/review.py', 'src/storycg/reference.py', 'src/storycg/tiers.py',
         'src/storycg/cycle.py', 'src/storycg/detection_review.py', 'src/storycg/eye_sampling.py',
         'src/storycg/detect.py', 'src/storycg/calibration.py', 'src/storycg/calibration_v2.py',
         'config/default.json', 'config/detection_review.json', 'config/detect.json',
         'config/identity_rules.json', 'models/detect/face_yolov8s.pt',
         'models/detect/person_yolov8s-seg.pt', 'tools/p9_04_calibrate.py']
LABELS = {'v1': 'verify/P4-labels.json', 'v2': 'verify/P9-labels.json'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes():
    return {p: sha(ROOT / p) for p in FILES}


def prompts():
    result = {}
    for p in FILES:
        if p.endswith('.py'):
            literals = [n.value for n in ast.walk(ast.parse((ROOT/p).read_text()))
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            result[p] = {'sha256': hashlib.sha256(json.dumps(literals, ensure_ascii=False).encode()).hexdigest(),
                         'literal_texts': literals}
    return result


def inputs():
    samples = {}
    references = json.loads((ROOT/'calibration/spec.json').read_text())['references']
    for cohort, label_path in LABELS.items():
        labels = json.loads((ROOT/label_path).read_text())
        directory = ROOT/('calibration/samples' if cohort == 'v1' else 'calibration/v2/samples')
        for sid, label in labels['samples'].items():
            if not label['confirmed']:
                continue
            meta = directory/f'{sid}.json'
            sample = json.loads(meta.read_text())
            image = directory/f'{sid}.png'
            if sha(image) != sample['image_sha256']:
                raise ValueError(f'image hash mismatch: {cohort}:{sid}')
            reference = ROOT/(references[sample['character_group']] if cohort == 'v1' else sample['reference_image'])
            if cohort == 'v2' and sha(reference) != sample['reference_sha256']:
                raise ValueError('reference hash mismatch')
            samples[f'{cohort}:{sid}'] = {'cohort': cohort, 'sample_id': sid, 'truth': label['truth'],
                'label_note': label.get('note'), 'metadata': str(meta.relative_to(ROOT)), 'metadata_sha256': sha(meta),
                'image': str(image.relative_to(ROOT)), 'image_sha256': sha(image),
                'reference': str(reference.relative_to(ROOT)), 'reference_sha256': sha(reference)}
    return samples


def freeze():
    path = CAL/'reviewer_freeze.json'
    if path.exists():
        raise ValueError('refuse replacing freeze')
    shutil.copyfile(ROOT/'config/review_reliability.json', CAL/'review_reliability_v1.json')
    samples = inputs()
    save_json_atomic(path, {'at': datetime.now(timezone.utc).isoformat(), 'files': hashes(),
        'prompt_texts': prompts(), 'labels_sha256': {c: sha(ROOT/p) for c,p in LABELS.items()},
        'samples': samples, 'v1_reliability_sha256': sha(CAL/'review_reliability_v1.json'),
        'rules': {'minimum_error_samples':3, 'minimum_ok_samples':3, 'fail_detection_min':.75,
                  'false_positive_max':.20, 'visual_repeats':2, 'detector_scored_repeats':1},
        'expected_visual_calls':len(samples)*4})
    print('frozen',len(samples),'samples; visual calls',len(samples)*4)


def check():
    frozen = json.loads((CAL/'reviewer_freeze.json').read_text())
    if frozen['files'] != hashes() or frozen['prompt_texts'] != prompts() or frozen['samples'] != inputs():
        raise ValueError('frozen reviewer or samples changed')
    if frozen['labels_sha256'] != {c: sha(ROOT/p) for c,p in LABELS.items()}:
        raise ValueError('labels changed')
    if sha(CAL/'review_reliability_v1.json') != frozen['v1_reliability_sha256']:
        raise ValueError('v1 reliability copy changed')
    return frozen


def run():
    frozen = check()
    output = CAL/'results.json'
    if output.exists() or (CAL/'P9-04-started.json').exists():
        raise ValueError('batch already started; no reruns')
    save_json_atomic(CAL/'P9-04-started.json', {'at': datetime.now(timezone.utc).isoformat()})
    config = json.loads((ROOT/'config/default.json').read_text())
    config['review_reliability_file'] = str(CAL/'review_reliability_v1.json')
    started = time.monotonic()
    vlm = review.VisionClient(CAL, config['model_service_url'], max_calls=260, max_seconds=6000)
    result = {'version':2, 'freeze':frozen['files'], 'samples':frozen['samples'], 'records':[],
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
            observations = detection_review.audit_detection(target,sample['identity'],entry,
                reference_path=reference, review_config=config)
            repeated = detection_review.audit_detection(target,sample['identity'],entry,
                reference_path=reference, review_config=config)
            same = observations == repeated
            result['detector_repeat_checks'].append({'sample':sample_id,'identical':same,
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


def render(result):
    pct = lambda value: '—' if value is None else f'{value:.1%}'
    lines = ['# P9-04 审核器 v2 校准', '',
        '所有 confirmed 样本，ambiguous 与未提供真值不统计。视觉项两次；检测项一次计分，重新检测一次验证确定性。',
        '检出只计 fail；error/ok 各 ≥3、检出 ≥75%、误报 ≤20% 才 reliable。', '',
        '| 项 | v1 检出/误报 | v2 在 v1 集 检出/误报 | v2 在 v2 集 检出/误报 | 合并 error/ok | 合并检出/误报 | 不可见判fail | 两次不一致 | 可靠性 |',
        '|---|---|---|---|---|---|---|---|---|']
    for item,row in result['summary'].items():
        rate = lambda r: pct(r.get('fail_detection_rate'))+'/'+pct(r.get('false_positive_rate'))
        lines.append(f"| {item} | {rate(result['v1_baseline'].get(item,{}))} | {rate(result['cohort_summary']['v1'][item])} | {rate(result['cohort_summary']['v2'][item])} | {row['error_samples']}/{row['ok_samples']} | {rate(row)} | {row['invisible_fail']}/{row['invisible_total']} ({pct(row['not_visible_fail_rate'])}) | {row['unstable_pairs']}/{row['total_pairs']} | {row['status']} |")
    lines += ['', '## 不可见误判与重复一致性：v1 对比', '',
              '| 项 | v1 不可见判fail | v2 在 v1 集 不可见判fail | v1 两次不一致 | v2 在 v1 集 两次不一致 |',
              '|---|---|---|---|---|']
    for item, baseline in result['v1_baseline'].items():
        current = result['cohort_summary']['v1'][item]
        lines.append(f"| {item} | {baseline['counts']['not_visible'].get('fail',0)}/{baseline['invisible_total']} | {current['invisible_fail']}/{current['invisible_total']} | {baseline['unstable_pairs']}/{baseline['total_pairs']} | {current['unstable_pairs']}/{current['total_pairs']} |")
    for cohort, rows in result['cohort_summary'].items():
        lines += ['', f'## v2 审核器在 {cohort} 样本集的逐项统计', '',
                  '| 项 | error/ok 样本 | error fail/minor/not_visible/pass | ok fail/minor/not_visible/pass | 不可见判fail | 两次不一致 | 可靠性 |',
                  '|---|---|---|---|---|---|---|']
        for item,row in rows.items():
            counts = lambda truth: '/'.join(str(row['counts'][truth].get(k,0)) for k in ('fail','minor','not_visible','pass'))
            lines.append(f"| {item} | {row['error_samples']}/{row['ok_samples']} | {counts('error')} | {counts('ok')} | {row['invisible_fail']}/{row['invisible_total']} | {row['unstable_pairs']}/{row['total_pairs']} | {row['status']} |")
    lines += ['', 'v1 与 v2 中相同图片的不同条目仍按各自 confirmed 样本计数；未增删或重选样本。',
              'v1 accessories 按旧真值映射合并发饰与配饰；v2 accessories 单独映射配饰，hair_ornaments 单列。',
              '配饰粗检使用 accessories 真值，仅检显著颜色，不能证明形状或存在正确。',
              '检测不可见不自动推断缺失。not_visible 不通过普通交付审核，在层级判定不构成 fail 否决。',
              '', '用量：', '```json', json.dumps(result['usage'],ensure_ascii=False,indent=2),'```',
              '', '原始响应：review_log.jsonl；逐项映射与 detector 重跑证据：results.json。']
    (CAL/'summary.md').write_text('\n'.join(lines)+'\n')


if __name__ == '__main__':
    {'freeze':freeze,'check':check,'run':run}[sys.argv[1]]()
