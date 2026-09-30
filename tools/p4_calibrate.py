"""Freeze first, then one serial calibration batch. No image generation or retries."""
import ast
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from storycg import review
from storycg.calibration import summarize, mapped_status, ITEMS
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / 'calibration'
FILES = ['src/storycg/review.py', 'src/storycg/reference.py', 'src/storycg/calibration.py',
         'config/default.json', 'tools/p4_calibrate.py']


def hashes():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in FILES}


def prompt_texts():
    # All literals include every inline prompt/schema description, without missing fragments.
    result = {}
    for p in [*FILES[:2], 'tools/p4_calibrate.py']:
        tree = ast.parse((ROOT / p).read_text())
        literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        text = json.dumps(literals, ensure_ascii=False)
        result[p] = {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'literal_texts': literals}
    return result


def check_freeze():
    frozen = json.loads((CAL / 'reviewer_freeze.json').read_text())
    if frozen['files'] != hashes() or frozen['prompt_texts'] != prompt_texts():
        raise ValueError('frozen reviewer changed')
    return frozen


def freeze():
    path = CAL / 'reviewer_freeze.json'
    if path.exists():
        raise ValueError('refuse to replace freeze')
    save_json_atomic(path, {'at': datetime.now(timezone.utc).isoformat(), 'files': hashes(),
        'prompt_texts': prompt_texts(), 'labels_sha256': hashlib.sha256((ROOT / 'verify/P4-labels.json').read_bytes()).hexdigest(),
        'rule': 'no reviewer/config/prompt/statistics changes after freeze; one batch, two runs per confirmed sample'})


def run():
    frozen = check_freeze()
    output = CAL / 'results.json'
    if output.exists() or (CAL / 'P4-02-started.json').exists():
        raise ValueError('calibration already started; do not rerun')
    labels_path = ROOT / 'verify/P4-labels.json'
    labels = json.loads(labels_path.read_text())
    if hashlib.sha256(labels_path.read_bytes()).hexdigest() != frozen['labels_sha256']:
        raise ValueError('labels changed')
    save_json_atomic(CAL / 'P4-02-started.json', {'at': datetime.now(timezone.utc).isoformat()})
    config = json.loads((ROOT / 'config/default.json').read_text())
    references = json.loads((CAL / 'spec.json').read_text())['references']
    records = []
    started = time.monotonic()
    vlm = review.VisionClient(CAL, config['model_service_url'], max_calls=200, max_seconds=4800)
    try:
        for sample_id, label in labels['samples'].items():
            if not label['confirmed']:
                continue
            sample = json.loads((CAL / 'samples' / f'{sample_id}.json').read_text())
            target = CAL / 'samples' / f'{sample_id}.png'
            if hashlib.sha256(target.read_bytes()).hexdigest() != sample['image_sha256']:
                raise ValueError('sample image hash mismatch')
            dest = CAL / 'P4-02-artifacts' / sample_id
            dest.mkdir(parents=True, exist_ok=True)
            reference = dest / 'reference.png'
            reference_source = ROOT / references[sample['character_group']]
            shutil.copyfile(reference_source, reference)
            entry = sample['shot_requirement']
            checks = review.identity_checklist(sample['identity'], entry['outfit'])
            for repeat in (1, 2):
                prefix = f'{sample_id}_run{repeat}'
                single = review.audit_single(vlm, target, entry, sample['props'], config, prefix + '_single')
                bbox = vlm.call(prefix + '_bbox', review.messages(
                    'Locate main protagonist face including ears and chin. Return normalized '
                    '[left, top, right, bottom]. If no identifiable face use found=false, bbox=[0,0,0,0].', [target]),
                    review.BBOX_SCHEMA, [target])
                paths = [reference, target]
                if bbox['status'] == 'pass' and bbox['data']['found']:
                    target_face = dest / f'target_face_{repeat}.png'
                    review.crop_face(target, bbox['data']['bbox'], target_face)
                    # The approved portrait's existing face crop, never a new generation.
                    portrait = json.loads(reference_source.parents[1].joinpath('portrait.json').read_text())
                    reference_face = dest / 'reference_face.png'
                    source_root = reference_source.parents[1]
                    shutil.copyfile(source_root / portrait['face']['path'], reference_face)
                    pair = dest / f'face_pair_{repeat}.png'
                    review.face_pair(reference_face, target_face, pair)
                    paths.append(pair)
                consistency = review.audit_consistency(vlm, paths, checks, config, prefix + '_consistency')
                record = {'sample': sample_id, 'repeat': repeat, 'single': single, 'bbox': bbox,
                          'consistency': consistency, 'reference_source': str(reference_source.relative_to(ROOT)),
                          'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(), 'visibility': review.visibility_summary([consistency]),
                          'layer_conclusions': {'single': review.conclude(review.result_statuses(single)),
                                               'consistency': review.conclude(review.result_statuses(consistency))}}
                record['mapped'] = {item: mapped_status(item, single, consistency) for item in ITEMS}
                records.append(record)
                save_json_atomic(output, {'freeze': frozen['files'], 'records': records,
                    'usage': {'generated_images': 0, 'review_calls': vlm.calls,
                              'elapsed_seconds': time.monotonic() - started}, 'complete': False})
                print(prefix, record['layer_conclusions'], 'calls', vlm.calls, flush=True)
    finally:
        vlm.close()
    check_freeze()
    rows = summarize(labels, records)
    result = json.loads(output.read_text())
    result.update(complete=True, summary=rows,
                  excluded={k: v['note'] for k, v in labels['samples'].items() if not v['confirmed']})
    save_json_atomic(output, result)
    save_json_atomic(ROOT / 'config/review_reliability.json', {'version': 1, 'items': rows,
        'rule': 'error_samples>=3 and ok_samples>=3; fail_detection>=0.75 and false_positive<=0.20 => reliable; insufficient => unverified; otherwise unreliable',
        'freeze': frozen['files']})
    render(result)


def render(result):
    lines = ['# P4-02 逐项校准', '',
        '仅 confirmed 样本，每个样本两层各运行两次。ambiguous 与缺失真值不统计。',
        '检出率分母为 error 样本项的两次观测；样本门槛按不同样本数。解析/超时失败映射为 fail，另见原始记录。', '',
        '| 项 | error/ok 样本 | error: fail/minor/not_visible/pass | 非pass检出率 | fail检出率 | ok误报率 | 不可见却pass | 不稳定项对 | 可靠性 |',
        '|---|---|---|---|---|---|---|---|---|']
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    for item, row in result['summary'].items():
        counts = row['counts']['error']
        lines.append(f"| {item} | {row['error_samples']}/{row['ok_samples']} | " +
                     '/'.join(str(counts.get(k, 0)) for k in ('fail', 'minor', 'not_visible', 'pass')) +
                     f" | {pct(row['nonpass_detection_rate'])} | {pct(row['fail_detection_rate'])} | {pct(row['false_positive_rate'])} | {row['invisible_pass']}/{row['invisible_total']} | {row['unstable_pairs']}/{row['total_pairs']} | {row['status']} |")
    unstable = sum(r['layer_conclusions'] != next(q for q in result['records'] if q['sample'] == r['sample'] and q['repeat'] == 1)['layer_conclusions'] for r in result['records'] if r['repeat'] == 2)
    pairs = len(result['records']) // 2
    lines += ['', f'两次层结论不一致：{unstable}/{pairs} ({unstable/pairs:.1%})。', '', '剔除样本：']
    lines += [f"- {k}: {v}" for k, v in result['excluded'].items()]
    lines += ['', '用量：', '```json', json.dumps(result['usage'], ensure_ascii=False, indent=2), '```',
              '', '各样本逐次原始结论：results.json；原始服务响应：review_log.jsonl（按 call_id 对照）。',
              'unreliable/unverified 的 pass 不可作为交付已验证依据。']
    (CAL / 'summary.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    {'freeze': freeze, 'run': run, 'check': check_freeze}[sys.argv[1]]()
