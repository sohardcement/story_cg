"""Offline scoring/publication safety tests; synthetic records are never calibration."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest

SCRIPT = Path(__file__).with_name('p10_03_calibrate.py')


def driver():
    if not SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location('external_driver', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def samples():
    return {f'{kind}{i}': {'level': level, 'truth': {'extra_protagonist': truth}}
            for kind, truth, level, size in [('err', 'error', 'candidate', 3),
                ('good', 'ok', 'candidate', 3), ('tiny', 'error', 'system_only', 3)]
            for i in range(size)}


def records(labels):
    return [{'sample': key, 'repeat': repeat, 'mapped':
             'fail' if label['truth']['extra_protagonist'] == 'error' and
             label['level'] == 'candidate' else 'pass', 'execution_status': 'complete'}
            for key, label in labels.items() for repeat in (1, 2)]


def test_candidate_status_excludes_system_only_but_reports_all():
    module = driver()
    assert module is not None, 'external driver not implemented'
    labels = samples()
    summary = module.summarize_external(labels, records(labels))
    assert summary['candidate']['status'] == 'reliable'
    assert summary['candidate']['fail_detection_rate'] == 1
    assert summary['system']['fail_detection_rate'] == .5
    assert summary['system']['error_samples'] == 6


def test_failed_calls_are_not_image_verdicts_or_visibility():
    module = driver()
    assert module is not None, 'external driver not implemented'
    labels = samples()
    rows = records(labels)
    rows[0].update(mapped=None, execution_status='transport_failed')
    summary = module.summarize_external(labels, rows)
    assert summary['candidate']['status'] == 'unverified'
    assert summary['candidate']['excluded_runs'] == 1
    assert summary['candidate']['counts']['error'] == {'fail': 5}
    assert summary['candidate']['coverage_complete'] is False


def test_publication_changes_only_extra_item_and_embeds_system_limit(tmp_path):
    module = driver()
    assert module is not None, 'external driver not implemented'
    baseline = {'version': 3, 'items': {'extra_protagonist': {'status': 'unreliable'},
                'other': {'status': 'reliable', 'note': 'kept'}}, 'rule': {'fixed': True},
                'freeze': {'old': 'hash'}, 'calibration_results': 'prior'}
    labels = samples()
    summary = module.summarize_external(labels, records(labels))
    target = tmp_path / 'reliability.json'
    module.publish_config(baseline, summary, target)
    published = json.loads(target.read_text())
    expected = copy.deepcopy(baseline)
    expected['items']['extra_protagonist'] = published['items']['extra_protagonist']
    assert published == expected
    item = published['items']['extra_protagonist']
    assert item['status'] == 'reliable'
    assert '50.00%' in item['note']
    assert '检测器看不到的小号人物仍会漏' in item['note']


def test_scoring_rejects_duplicate_or_missing_rounds():
    module = driver()
    assert module is not None, 'external driver not implemented'
    labels = samples()
    rows = records(labels)
    with pytest.raises(ValueError):
        module.summarize_external(labels, rows + [rows[0]])
    with pytest.raises(ValueError):
        module.summarize_external(labels, rows[:-1])


@pytest.mark.parametrize('error,raw,expected', [
    ('ReadTimeout: timed out', None, True),
    ('HTTPStatusError: 503 Service Unavailable', None, True),
    ('HTTPStatusError: 400 Bad Request', None, False),
    ('ValueError: incomplete', '{"choices":[]}', True),
    ('ValueError: incomplete', '{"choices":[{"message":{"content":""}}]}', True),
    ('ValidationError: invalid grade', '{"choices":[{"message":{"content":"{}"}}]}', False),
    ('JSONDecodeError: invalid', 'invalid', False),
    ('review budget exhausted', '', False),
])
def test_retry_only_transport_failures(error, raw, expected):
    module = driver()
    assert module.is_transport({'status': 'fail', 'error': error}, raw) is expected
