"""Replay successful supplementary responses as development-only evidence.

This tool has no detector, vision-client or generation calls. Candidate geometry
comes from the original scored detection; response keys always remain original.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from storycg import detection_review, review_v3
from storycg.calibration_v3 import stats
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]


def object_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def candidate_subset(context, raw_data, new_candidates):
    """Match identities, never positions, before removing obsolete candidates."""
    original = context['candidates']
    by_identity = {(candidate['kind'], candidate['index']): candidate for candidate in original}
    assert len(by_identity) == len(original), 'duplicate original candidate identity'
    keys = [candidate['key'] for candidate in original]
    assert len(set(keys)) == len(keys), 'duplicate original candidate key'
    assert set(raw_data['candidates']) == set(keys), 'raw_data candidate keys differ from request context'
    new_by_identity = {(candidate['kind'], candidate['index']): candidate for candidate in new_candidates}
    assert len(new_by_identity) == len(new_candidates), 'duplicate new candidate identity'
    added = [copy.deepcopy(candidate) for identity, candidate in new_by_identity.items()
             if identity not in by_identity]
    for identity, candidate in new_by_identity.items():
        if identity not in by_identity:
            continue
        for field in ('bbox', 'confidence', 'area_ratio', 'below_count_threshold'):
            if field in candidate:
                assert candidate[field] == by_identity[identity][field], f'candidate {field} changed'
    retained = [candidate for candidate in original
                if (candidate['kind'], candidate['index']) in new_by_identity]
    retained_keys = {candidate['key'] for candidate in retained}
    removed_keys = [key for key in keys if key not in retained_keys]
    prepared = copy.deepcopy(context)
    prepared['candidates'] = copy.deepcopy(retained)
    for field in ('index_map', 'crops'):
        if field in prepared:
            prepared[field] = {key: value for key, value in prepared[field].items() if key not in removed_keys}
    data = copy.deepcopy(raw_data)
    data['candidates'] = {key: value for key, value in data['candidates'].items() if key in retained_keys}
    assert all((candidate['kind'], candidate['index']) in by_identity
               for candidate in prepared['candidates']), 'new candidate entered historical response'
    return prepared, data, removed_keys, added


def recompute(source, events, *, cohort='v3', item='extra_protagonist', sample_ids=None):
    """Validate original provenance and derive a separate development summary."""
    started = time.monotonic()
    selected = {key: label for key, label in source['samples'].items()
                if label['cohort'] == cohort and item in label.get('v3_truth', {})}
    if sample_ids is not None:
        wanted = set(sample_ids)
        available = {label['sample_id'] for label in selected.values()}
        if wanted - available:
            raise ValueError(f'unknown sample_id for cohort/item: {sorted(wanted - available)}')
        selected = {key: label for key, label in selected.items() if label['sample_id'] in wanted}
    if not selected:
        raise ValueError(f'no labeled samples for cohort={cohort!r}, item={item!r}')
    requests, responses = {}, {}
    for event in events:
        event_type = event.get('event')
        if event_type not in ('request', 'response'):
            continue
        indexed = requests if event_type == 'request' else responses
        call_id = event['call_id']
        assert call_id not in indexed, f'duplicate {event_type} call_id: {call_id}'
        indexed[call_id] = event
    detections = {}
    for check in source['detector_repeat_checks']:
        if check['sample'] not in selected:
            continue
        assert check['sample'] not in detections, 'duplicate scored detector record'
        assert check['identical'], 'original detector repeat was not identical'
        detections[check['sample']] = check['first']['detection']
    records = []
    used_calls = set()
    for sample, label in selected.items():
        originals = [row for row in source['records'] if row['sample'] == sample]
        assert len(originals) == 2 and {row['repeat'] for row in originals} == {1, 2}, \
            f'{sample}: requires two distinct successful repeats'
        assert sample in detections, f'{sample}: missing original scored detection'
        entry = label['state']['shot_requirement']
        shot = entry.get('shot', entry)
        new_candidates = review_v3.extra_candidates(detections[sample], shot)
        for row in sorted(originals, key=lambda original: original['repeat']):
            original = row['v3']
            assert original['status'] == 'pass', f'{sample}: original response was not successful'
            call_id = original['call_id']
            assert call_id not in used_calls, f'duplicate successful call_id: {call_id}'
            used_calls.add(call_id)
            assert call_id in requests, f'missing original request: {call_id}'
            assert call_id in responses, f'missing original response: {call_id}'
            request, response = requests[call_id], responses[call_id]
            assert response['status'] == 'pass', f'{call_id}: logged response was not successful'
            assert request['purpose'] == response['purpose'] == original['purpose'], 'request/response purpose mismatch'
            choice = json.loads(response['raw_response'])['choices'][0]
            assert choice.get('finish_reason') in (None, 'stop'), 'incomplete original response'
            raw_data = json.loads(choice['message']['content'])
            schema = request['request']['response_format']['json_schema']['schema']
            Draft202012Validator(schema).validate(raw_data)
            assert raw_data == original['raw_data'], f'{call_id}: raw_data differs from successful response'
            text = request['request']['messages'][0]['content'][0]['text']
            assert text.startswith(review_v3.PROMPT), 'original request prompt changed'
            context = json.loads(text[len(review_v3.PROMPT):])
            assert context['index_map'] == original['image_index_map'], 'request image index map mismatch'
            assert context['reflection'] == detection_review.declares_reflection(shot), 'request/state reflection mismatch'
            context['crops'] = copy.deepcopy(original['crop_evidence'])
            prepared, data, removed_keys, added = candidate_subset(context, raw_data, new_candidates)
            normalized = review_v3.normalize({'status': 'pass', 'data': data}, prepared, {})
            new_mapped = normalized['data'][item]['status']
            records.append({'sample': sample, 'sample_id': label['sample_id'], 'repeat': row['repeat'],
                            'truth': label['v3_truth'][item], 'source_call_id': call_id,
                            'old_mapped': row['v3_mapped'][item], 'new_mapped': new_mapped,
                            'v3_mapped': {item: new_mapped}, 'v3': normalized,
                            'original_candidates': copy.deepcopy(context['candidates']),
                            'retained_candidates': copy.deepcopy(prepared['candidates']),
                            'removed_candidate_keys': removed_keys,
                            'added_candidates_without_response': added,
                            'source_raw_data_sha256': object_sha(raw_data),
                            'source_request_context_sha256': object_sha(context),
                            'source_detection_sha256': object_sha(detections[sample])})
    statistics = stats(selected, records, item)
    statistics.pop('status')
    return {'version': '3.1', 'source_version': source['version'],
            'scope': 'development_only', 'excluded_from_release': True,
            'historical_subset_only': True, 'not_equivalent_to_full_v3_1_audit': True,
            'release_use': 'Development observations cannot determine reliability or publication status.',
            'limitations': ['Only the intersection of original and new candidate identities is normalized.',
                            'New candidates without original responses are recorded but are not audited or scored as candidates.',
                            'An empty historical candidate subset uses the existing image-level ok aggregation.',
                            'Input labels must contain embedded shot state; older metadata-only cohorts are unsupported.'],
            'cohort': cohort, 'item': item, 'samples': copy.deepcopy(selected),
            'records': records, 'statistics': {item: statistics},
            'usage': {'review_calls': 0, 'detector_calls': 0, 'generated_images': 0},
            'elapsed_seconds': time.monotonic() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'calibration/v3/results.json')
    parser.add_argument('--log', type=Path, default=ROOT / 'calibration/v3/review_log.jsonl')
    parser.add_argument('--output', type=Path, default=ROOT / 'calibration/v3.1/development_results.json')
    parser.add_argument('--cohort', default='v3')
    parser.add_argument('--item', default='extra_protagonist')
    parser.add_argument('--sample-id', action='append', dest='sample_ids')
    args = parser.parse_args()
    paths = [path.resolve() for path in (args.source, args.log, args.output)]
    if not all(path.is_relative_to(ROOT) for path in paths):
        parser.error('input and output paths must remain in the working directory')
    source_path, log_path, output_path = paths
    relative_output = output_path.relative_to(ROOT)
    if output_path in (source_path, log_path) or relative_output.parts[0] not in ('calibration', 'reports'):
        parser.error('output must be a separate development artifact in calibration/ or reports/')
    result = recompute(json.loads(source_path.read_text()),
                       [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()],
                       cohort=args.cohort, item=args.item, sample_ids=args.sample_ids)
    result['created_at'] = datetime.now(timezone.utc).isoformat()
    result['provenance'] = {name: {'path': str(path.relative_to(ROOT)),
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                            for name, path in [('results', source_path), ('review_log', log_path),
                                               ('reviewer', ROOT / 'src/storycg/review_v3.py'),
                                               ('execution_tool', Path(__file__).resolve())]}
    save_json_atomic(output_path, result)
    print(json.dumps({'output': str(relative_output), 'scope': result['scope'],
                      'samples': len(result['samples']), 'records': len(result['records']),
                      'statistics': result['statistics'], 'usage': result['usage']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
