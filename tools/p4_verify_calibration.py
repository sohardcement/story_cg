"""Read-only verification of freeze, sample selection, statistics and raw responses."""
import copy
import hashlib
import json
from pathlib import Path
from storycg import review
from storycg.calibration import summarize
from p4_calibrate import check_freeze

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / 'calibration'


def verify():
    check_freeze()
    result = json.loads((CAL / 'results.json').read_text())
    assert result['complete'] is True
    labels = json.loads((ROOT / 'verify/P4-labels.json').read_text())
    confirmed = {k for k, v in labels['samples'].items() if v['confirmed']}
    assert {r['sample'] for r in result['records']} == confirmed
    assert len(result['records']) == len(confirmed) * 2
    for sample in confirmed:
        assert sorted(r['repeat'] for r in result['records'] if r['sample'] == sample) == [1, 2]
    logs = [json.loads(line) for line in (CAL / 'review_log.jsonl').read_text().splitlines()]
    requests = {r['call_id']: r for r in logs if r['event'] == 'request'}
    responses = {r['call_id']: r for r in logs if r['event'] == 'response'}
    references = json.loads((CAL / 'spec.json').read_text())['references']
    config = json.loads((ROOT / 'config/default.json').read_text())
    conversions = parsed = failures = 0
    for record in result['records']:
        sample = json.loads((CAL / 'samples' / f"{record['sample']}.json").read_text())
        assert record['reference_source'] == references[sample['character_group']]
        assert record['reference_sha256'] == hashlib.sha256((ROOT / record['reference_source']).read_bytes()).hexdigest()
        assert record['consistency']['checklist'] == review.identity_checklist(sample['identity'], sample['shot_requirement']['outfit'])
        for layer in ('single', 'bbox', 'consistency'):
            actual = record[layer]
            response = responses[actual['call_id']]
            assert response['status'] == actual['status']
            assert requests[actual['call_id']]['purpose'] == actual['purpose']
            if actual['status'] != 'pass':
                failures += 1
                assert actual['error'] == response['error']
                continue
            raw = json.loads(json.loads(response['raw_response'])['choices'][0]['message']['content'])
            if layer == 'bbox':
                if raw['found']:
                    path = CAL / requests[actual['call_id']]['request']['messages'][0]['content'][1]['image_url']['url']['path']
                    raw['bbox'], converted = review.normalize_bbox(raw['bbox'], path)
                    conversions += converted
                assert raw == actual['data']
            else:
                expected = {'status': 'pass', 'data': copy.deepcopy(raw)}
                review.enforce_visibility(expected)
                if layer == 'single' and 'framing_observation' in expected['data']:
                    observed = expected['data'].pop('framing_observation')
                    sample = json.loads((CAL / 'samples' / f"{record['sample']}.json").read_text())
                    expected['data']['framing'] = review.framing_grade(sample['shot_requirement']['shot']['framing'], observed, config)
                    assert actual['framing_observation'] == observed
                projected = {k: {field: value for field, value in item.items()
                                   if field not in ('reliability', 'verified', 'reliability_note')}
                             for k, item in actual['data'].items()}
                assert expected['data'] == projected
            parsed += 1
    rows = summarize(labels, result['records'])
    assert rows == result['summary']
    reliability = json.loads((ROOT / 'config/review_reliability.json').read_text())
    assert reliability['items'] == rows
    assert result['usage']['review_calls'] == len(result['records']) * 3
    # Exercise the frozen runtime annotation code against the generated real config.
    for record in result['records']:
        for layer in ('single', 'consistency'):
            annotated = review.annotate_reliability(copy.deepcopy(record[layer]), review.load_reliability(config))
            for item in annotated.get('data', {}).values():
                if item['reliability'] != 'reliable':
                    assert item['reliability_note'] == '该项未经可靠审核'
                    assert item['verified'] is False
    outcome = {'freeze_matches': True, 'confirmed_samples': len(confirmed),
               'raw_successes_matched': parsed, 'raw_failures_matched': failures,
               'bbox_conversions': conversions, 'review_calls': result['usage']['review_calls'],
               'statistics_reproduced': True, 'runtime_annotations_checked': True}
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return outcome


if __name__ == '__main__':
    verify()
