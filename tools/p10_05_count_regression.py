"""Fresh offline D28 CPU counts against immutable P9-04/P10-02 registries.

Writes only under reports/assets/P10-05, blocks socket connections, and never
instantiates a generation or vision client. Saved detections are comparisons only.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import socket
import time
from datetime import datetime, timezone

from storycg import detect, detection_review

ROOT = Path(__file__).resolve().parents[1]
CALIBRATIONS = {'P9-04': 'v2', 'P10-02': 'v3'}
REGRESSION_IMAGES = [
    'jobs/cg-650f8f818fa4/storycg_cg-650f8f818fa4_shot01_a001.png',
    'jobs/cg-650f8f818fa4/storycg_cg-650f8f818fa4_shot01_a002.png',
]


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def count_grade(observation):
    return detection_review.review_detection_items(
        {'protagonist_count': observation},
        {'protagonist_count': {'status': 'reliable'}},
    )['data']['protagonist_count']['status']


def statistics(rows, field):
    counts = {truth: {} for truth in ('error', 'ok', 'not_visible')}
    for row in rows:
        grade = row[field]['grade_status']
        values = counts[row['truth']]
        values[grade] = values.get(grade, 0) + 1
    errors = sum(counts['error'].values())
    oks = sum(counts['ok'].values())
    return {
        'error_samples': errors, 'ok_samples': oks,
        'not_visible_samples': sum(counts['not_visible'].values()),
        'counts': counts,
        'fail_detection_rate': counts['error'].get('fail', 0) / errors if errors else None,
        'false_positive_rate': counts['ok'].get('fail', 0) / oks if oks else None,
    }


def run(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'reports/assets/P10-05'):
        raise ValueError('output must be under reports/assets/P10-05')
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'count-regression.json').exists():
        raise ValueError('use a new output directory; preserve earlier evidence')
    started = time.monotonic()
    blocked = []

    def no_connections(*args, **kwargs):
        blocked.append(repr(args[1:] if len(args) > 1 else args))
        raise RuntimeError('D28 regression forbids network connections')

    socket.socket.connect = no_connections
    socket.socket.connect_ex = no_connections
    socket.create_connection = no_connections
    socket.getaddrinfo = no_connections
    cfg = detection_review.settings()
    old_cfg = copy.deepcopy(cfg)
    old_cfg['count']['face_confidence'] = .6
    assert cfg['count'] == {'face_confidence': .45, 'relative_face_area': .3,
                            'person_confidence': .5, 'person_area': .08}
    detector_cfg = detect.load_detect_config()
    for kind in ('faces', 'persons'):
        detector_cfg[kind]['model'] = str((ROOT / detector_cfg[kind]['model']).resolve())
    model_hashes = {detector_cfg[kind]['model']: sha(detector_cfg[kind]['model'])
                    for kind in ('faces', 'persons')}
    # detect.ROOT is used for the ultralytics runtime directory and model path
    # resolution. Explicit absolute model paths retain the original models;
    # move only the per-process runtime cache into this new evidence directory.
    detect.ROOT = output
    cache = {}

    def fresh(image):
        image = Path(image).resolve()
        if image not in cache:
            result = detect.detect_image(image, config=detector_cfg)
            name = f'count-detection-{len(cache) + 1:02d}.json'
            write(output / name, {'image': str(image.relative_to(ROOT)),
                                 'image_sha256': sha(image),
                                 'method': 'fresh_local_cpu_detector',
                                 'detection': detect.json_result(result)})
            cache[image] = (result, name)
        return cache[image]

    def observed(result, shot, settings):
        value = detection_review.protagonist_count(result['faces'], result['persons'],
                                                   shot, config=settings)
        value['grade_status'] = count_grade(value)
        return value

    cohorts = {}
    for order, version in CALIBRATIONS.items():
        base = ROOT / 'calibration' / version
        results = load(base / 'results.json')
        frozen = load(base / 'reviewer_freeze.json')
        manifest = load(base / 'manifest.json')
        assert results['samples'] == frozen['samples'], 'sample registry changed'
        samples = {sid: value for sid, value in results['samples'].items()
                   if 'protagonist_count' in value['truth']}
        detections = {row['sample']: row for row in results['detector_repeat_checks']}
        assert len(detections) == len(results['detector_repeat_checks'])
        rows = []
        for sid, sample in samples.items():
            image, metadata = ROOT / sample['image'], ROOT / sample['metadata']
            assert sha(image) == sample['image_sha256'], 'image hash changed'
            assert sha(metadata) == sample['metadata_sha256'], 'metadata hash changed'
            if version == 'v2':
                assert sample['sample_id'] in manifest['sample_ids']
            else:
                assert manifest['samples'][sid] == sample
            saved = detections[sid]['first']['protagonist_count']
            assert saved['thresholds'] == old_cfg['count']
            assert saved['protagonist_count'] == detections[sid]['repeat']['protagonist_count']['protagonist_count']
            entry = load(metadata)['shot_requirement']
            result, evidence_file = fresh(image)
            row = {'sample': sid, 'truth': sample['truth']['protagonist_count'],
                   'image': sample['image'], 'image_sha256': sha(image),
                   'detection_file': evidence_file,
                   'saved': saved,
                   'fresh_old_0_6': observed(result, entry.get('shot', entry), old_cfg),
                   'fresh_new_0_45': observed(result, entry.get('shot', entry), cfg)}
            rows.append(row)
            print(order, sid, 'saved/old/new', saved['protagonist_count'],
                  row['fresh_old_0_6']['protagonist_count'],
                  row['fresh_new_0_45']['protagonist_count'], flush=True)
        stats = {field: statistics(rows, field)
                 for field in ('saved', 'fresh_old_0_6', 'fresh_new_0_45')}
        baseline = results['summary']['protagonist_count']
        changes = [row for row in rows if len({
            (row[field]['protagonist_count'], row[field]['status'], row[field]['grade_status'])
            for field in stats}) != 1]
        cohorts[order] = {
            'registry': str((base / 'results.json').relative_to(ROOT)),
            'registry_sha256': sha(base / 'results.json'),
            'freeze_sha256': sha(base / 'reviewer_freeze.json'),
            'manifest_sha256': sha(base / 'manifest.json'),
            'total_registry_samples': len(results['samples']),
            'count_samples': len(samples), 'rows': rows, 'statistics': stats,
            'saved_summary': baseline, 'changed_images': changes,
            'statistics_match_saved': all(value == {key: baseline[key] for key in value}
                                          for value in stats.values()),
        }
    shot = load(ROOT / 'jobs/cg-650f8f818fa4/storyboard.json')['shots'][0]['shot']
    regression = []
    for path in REGRESSION_IMAGES:
        result, evidence_file = fresh(ROOT / path)
        old = observed(result, shot, old_cfg)
        new = observed(result, shot, cfg)
        regression.append({'image': path, 'image_sha256': sha(ROOT / path),
                           'detection_file': evidence_file,
                           'fresh_old_0_6': old, 'fresh_new_0_45': new})
        print(path, 'old/new', old['protagonist_count'], new['protagonist_count'], flush=True)
    passed = (all(row['fresh_new_0_45']['protagonist_count'] == 2 for row in regression)
              and all(not cohort['changed_images'] and cohort['statistics_match_saved']
                      for cohort in cohorts.values()) and not blocked)
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'offline_network_guard': True, 'blocked_network_attempts': blocked,
              'generated_images': 0, 'vision_calls': 0,
              'detector_unique_image_runs': len(cache),
              'detector_model_inferences': 2 * len(cache),
              'model_sha256': model_hashes, 'old_count_config': old_cfg['count'],
              'new_count_config': cfg['count'], 'calibrations': cohorts,
              'regression': regression, 'passed': passed,
              'elapsed_seconds': time.monotonic() - started}
    write(output / 'count-regression.json', report)
    print('PASS' if passed else 'FAIL', 'unique images', len(cache),
          'seconds', round(report['elapsed_seconds'], 3), flush=True)
    return passed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'reports/assets/P10-05/count-run')
    raise SystemExit(0 if run(parser.parse_args().output) else 1)
