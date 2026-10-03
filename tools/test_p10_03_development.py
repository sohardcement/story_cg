"""Offline tests for response provenance and development-only recomputation."""
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from storycg import review_v3


def tool():
    assert importlib.util.find_spec('p10_03_development') is not None, 'development replay tool is missing'
    return importlib.import_module('p10_03_development')


def detection(box, area):
    return {'bbox': box, 'area_ratio': area, 'confidence': .9}


def fixture():
    current = {'size': [100, 100],
               'faces': [detection([40, 20, 60, 40], .04)],
               'persons': [detection([20, 10, 70, 100], .45),
                           detection([78, 8, 88, 35], .027)]}
    candidate = {'kind': 'person', 'index': 1, **current['persons'][1],
                 'below_count_threshold': True, 'key': 'candidate_004'}
    context = {'index_map': {'reference_full': 0, 'current_full': 1, 'candidate_004': 2},
               'candidates': [candidate], 'reflection': False, 'checks': {},
               'locked_tags': [], 'outfit': {}}
    grade = {'status': 'ok', 'reference_observation': 'visible',
             'current_observation': 'visible', 'reason': 'matching pixels'}
    raw = {'outfit_facets': {key: copy.deepcopy(grade) for key in review_v3.FACETS},
           'accessories': copy.deepcopy(grade), 'hair_ornaments': copy.deepcopy(grade),
           'candidates': {'candidate_004': {'status': 'ok', 'is_reflection': False,
                          'reason': 'visible identity', 'hair_color': copy.deepcopy(grade),
                          'hairstyle': copy.deepcopy(grade), 'eye_color': copy.deepcopy(grade)}}}
    samples = {'cohort_a:entry_a': {'cohort': 'cohort_a', 'sample_id': 'entry_a',
               'v3_truth': {'extra_protagonist': 'error'},
               'state': {'shot_requirement': {'shot': {'reflection': False}}}},
               'cohort_b:entry_b': {'cohort': 'cohort_b', 'sample_id': 'entry_b',
               'v3_truth': {'extra_protagonist': 'ok'}}}
    events = []
    records = []
    for repeat in (1, 2):
        call_id = f'offline:{repeat}'
        purpose = f'development_fixture_{repeat}'
        request = {'messages': [{'role': 'user', 'content': [{'type': 'text',
                   'text': review_v3.PROMPT + json.dumps(context)}]}],
                   'response_format': {'json_schema': {'schema': review_v3.schema(context)}}}
        response = {'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(raw)}}]}
        events += [{'event': 'request', 'call_id': call_id, 'purpose': purpose, 'request': request},
                   {'event': 'response', 'call_id': call_id, 'purpose': purpose,
                    'status': 'pass', 'raw_response': json.dumps(response)}]
        records.append({'sample': 'cohort_a:entry_a', 'repeat': repeat,
                        'v3_mapped': {'extra_protagonist': 'pass'},
                        'v3': {'status': 'pass', 'purpose': purpose, 'call_id': call_id,
                               'raw_data': copy.deepcopy(raw), 'crop_evidence': {},
                               'image_index_map': copy.deepcopy(context['index_map'])}})
    source = {'version': 3, 'samples': samples, 'records': records,
              'detector_repeat_checks': [{'sample': 'cohort_a:entry_a', 'identical': True,
                                          'first': {'detection': current}}]}
    return source, events, context, raw


def test_candidate_subset_preserves_original_keys_and_input():
    _, _, context, raw = fixture()
    removed = {'kind': 'person', 'index': 7, 'key': 'candidate_000'}
    context['candidates'].insert(0, removed)
    context['index_map']['candidate_000'] = 3
    raw['candidates']['candidate_000'] = copy.deepcopy(raw['candidates']['candidate_004'])
    before = copy.deepcopy((context, raw))
    new_candidates = [{'kind': 'person', 'index': 1}]
    filtered, data, removed_keys, added = tool().candidate_subset(context, raw, new_candidates)
    assert [candidate['key'] for candidate in filtered['candidates']] == ['candidate_004']
    assert list(data['candidates']) == ['candidate_004']
    assert removed_keys == ['candidate_000']
    assert added == []
    assert 'candidate_000' not in filtered['index_map']
    assert (context, raw) == before


def test_candidate_subset_records_new_face_without_reusing_removed_person_response():
    _, _, context, raw = fixture()
    new_candidates = [{'kind': 'face', 'index': 1}]
    filtered, data, removed_keys, added = tool().candidate_subset(context, raw, new_candidates)
    assert filtered['candidates'] == []
    assert data['candidates'] == {}
    assert removed_keys == ['candidate_004']
    assert added == [{'kind': 'face', 'index': 1}]


def test_recompute_uses_original_responses_and_is_never_release_statistics():
    source, events, _, _ = fixture()
    before = copy.deepcopy((source, events))
    result = tool().recompute(source, events, cohort='cohort_a', item='extra_protagonist')
    assert list(result['samples']) == ['cohort_a:entry_a']
    assert [row['sample_id'] for row in result['records']] == ['entry_a', 'entry_a']
    assert [row['old_mapped'] for row in result['records']] == ['pass', 'pass']
    assert [row['new_mapped'] for row in result['records']] == ['fail', 'fail']
    assert list(result['records'][0]['v3']['raw_data']['candidates']) == ['candidate_004']
    assert result['statistics']['extra_protagonist']['counts']['error'] == {'fail': 2}
    assert 'status' not in result['statistics']['extra_protagonist']
    assert result['excluded_from_release'] is True
    assert result['historical_subset_only'] is True
    assert result['not_equivalent_to_full_v3_1_audit'] is True
    assert result['usage'] == {'review_calls': 0, 'detector_calls': 0, 'generated_images': 0}
    assert (source, events) == before


def test_recompute_rejects_success_record_not_bound_to_raw_response():
    source, events, _, _ = fixture()
    source['records'][0]['v3']['raw_data']['candidates']['candidate_004']['reason'] = 'changed'
    with pytest.raises(AssertionError, match='raw_data'):
        tool().recompute(source, events, cohort='cohort_a', item='extra_protagonist')


def test_recompute_rejects_unresolved_success_or_duplicate_call_ids():
    source, events, _, _ = fixture()
    with pytest.raises(AssertionError, match='response'):
        tool().recompute(source, [events[0], *events[2:]], cohort='cohort_a', item='extra_protagonist')
    with pytest.raises(AssertionError, match='duplicate'):
        tool().recompute(source, [*events, events[0]], cohort='cohort_a', item='extra_protagonist')


def test_recompute_requires_two_distinct_repeats_and_recognizes_sample_id_filter():
    source, events, _, _ = fixture()
    source['records'][1]['repeat'] = 1
    with pytest.raises(AssertionError, match='two distinct'):
        tool().recompute(source, events, cohort='cohort_a', item='extra_protagonist')
    with pytest.raises(ValueError, match='sample_id'):
        tool().recompute(source, events, cohort='cohort_a', item='extra_protagonist', sample_ids=['absent'])


def test_recompute_reports_new_unreviewed_face_and_scores_empty_historical_subset():
    source, events, context, _ = fixture()
    current = source['detector_repeat_checks'][0]['first']['detection']
    current['faces'].append(detection([80, 10, 84, 14], .001))
    current['persons'][1] = detection([19, 9, 90, 100], .65)
    for event in events:
        if event['event'] != 'request':
            continue
        recorded = copy.deepcopy(context)
        recorded['candidates'][0].update(current['persons'][1])
        event['request']['messages'][0]['content'][0]['text'] = review_v3.PROMPT + json.dumps(recorded)
    result = tool().recompute(source, events, cohort='cohort_a', item='extra_protagonist')
    assert [row['new_mapped'] for row in result['records']] == ['pass', 'pass']
    assert [row['removed_candidate_keys'] for row in result['records']] == [['candidate_004'], ['candidate_004']]
    assert [(candidate['kind'], candidate['index'])
            for candidate in result['records'][0]['added_candidates_without_response']] == [('face', 1)]
    assert result['records'][0]['v3']['raw_data']['candidates'] == {}
