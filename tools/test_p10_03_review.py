"""Offline evidence-mapping and candidate-filtering regression tests."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from storycg import review_v3 as v3


def feature(status):
    return {'status': status, 'reference_observation': 'reference pixels',
            'current_observation': 'current pixels', 'reason': 'visible comparison'}


def candidate(status='ok', hair='ok', hairstyle='ok', eyes='ok', reflection=False):
    return {'status': status, 'is_reflection': reflection, 'reason': 'candidate pixels',
            'hair_color': feature(hair), 'hairstyle': feature(hairstyle),
            'eye_color': feature(eyes)}


def raw_result(candidates):
    return {'status': 'pass', 'data': {
        'outfit_facets': {key: feature('not_visible') for key in v3.FACETS},
        'accessories': feature('not_visible'), 'hair_ornaments': feature('not_visible'),
        'candidates': {f'candidate_{i:03d}': item for i, item in enumerate(candidates)}}}


def normalize(raw, tmp_path, reflection=False):
    return v3.normalize(raw, {'reflection': reflection, 'crops': {}},
                        {'review_reliability_file': str(tmp_path / 'unused.json')})


@pytest.mark.parametrize('overall', ['ok', 'duplicate', 'not_visible'])
@pytest.mark.parametrize('eyes', ['ok', 'not_visible'])
def test_matching_hair_derives_duplicate_independently_of_overall_status(tmp_path, overall, eyes):
    raw = raw_result([candidate(status=overall, eyes=eyes)])
    result = normalize(raw, tmp_path)
    assert result['validated_candidates']['candidate_000']['status'] == 'duplicate'
    assert result['data']['extra_protagonist']['observed_status'] == 'duplicate'
    assert result['data']['extra_protagonist']['status'] == 'fail'


@pytest.mark.parametrize('hair,hairstyle,eyes', [
    ('mismatch', 'ok', 'ok'),
    ('ok', 'mismatch', 'ok'),
    ('ok', 'ok', 'mismatch'),
    ('not_visible', 'mismatch', 'ok'),
    ('mismatch', 'not_visible', 'ok'),
    ('ok', 'not_visible', 'mismatch'),
])
def test_any_identity_mismatch_prevents_duplicate_and_invisibility(tmp_path, hair, hairstyle, eyes):
    raw = raw_result([candidate(status='duplicate', hair=hair, hairstyle=hairstyle, eyes=eyes)])
    result = normalize(raw, tmp_path)
    assert result['validated_candidates']['candidate_000']['status'] == 'ok'
    assert result['data']['extra_protagonist']['observed_status'] == 'ok'
    assert result['data']['extra_protagonist']['status'] == 'pass'


@pytest.mark.parametrize('hair,hairstyle,eyes', [
    ('not_visible', 'ok', 'ok'),
    ('ok', 'not_visible', 'ok'),
    ('not_visible', 'not_visible', 'not_visible'),
])
def test_unseen_hair_without_conflicting_evidence_is_not_visible(tmp_path, hair, hairstyle, eyes):
    raw = raw_result([candidate(hair=hair, hairstyle=hairstyle, eyes=eyes)])
    result = normalize(raw, tmp_path)
    assert result['validated_candidates']['candidate_000']['status'] == 'not_visible'
    assert result['data']['extra_protagonist']['observed_status'] == 'not_visible'
    assert result['data']['extra_protagonist']['status'] == 'not_visible'


@pytest.mark.parametrize('declared,observed,expected', [
    (False, False, 'duplicate'), (False, True, 'duplicate'),
    (True, False, 'duplicate'), (True, True, 'ok'),
])
def test_only_declared_and_observed_reflection_allows_matching_candidate(tmp_path, declared, observed, expected):
    raw = raw_result([candidate(reflection=observed)])
    result = normalize(raw, tmp_path, declared)
    assert result['validated_candidates']['candidate_000']['status'] == expected
    assert result['data']['extra_protagonist']['observed_status'] == expected


def test_physical_duplicate_overrides_allowed_reflection_and_unseen_candidate(tmp_path):
    raw = raw_result([candidate(reflection=True), candidate(hair='not_visible'), candidate()])
    result = normalize(raw, tmp_path, reflection=True)
    assert result['data']['extra_protagonist']['observed_status'] == 'duplicate'
    assert result['data']['extra_protagonist']['status'] == 'fail'


def test_no_duplicate_preserves_existing_not_visible_aggregation(tmp_path):
    raw = raw_result([candidate(hair='mismatch'), candidate(hairstyle='not_visible')])
    result = normalize(raw, tmp_path)
    assert result['data']['extra_protagonist']['observed_status'] == 'not_visible'
    assert result['data']['extra_protagonist']['status'] == 'not_visible'


def test_empty_candidate_set_is_ok(tmp_path):
    result = normalize(raw_result([]), tmp_path)
    assert result['data']['extra_protagonist']['observed_status'] == 'ok'
    assert result['data']['extra_protagonist']['status'] == 'pass'


def test_normalization_preserves_raw_data_and_input(tmp_path):
    raw = raw_result([candidate(status='ok'), candidate(status='duplicate', eyes='mismatch')])
    before = copy.deepcopy(raw)
    result = normalize(raw, tmp_path)
    assert result['raw_data'] == before['data']
    assert raw == before
    assert result['validated_candidates']['candidate_000']['status'] == 'duplicate'
    assert result['validated_candidates']['candidate_001']['status'] == 'ok'
    result['validated_candidates']['candidate_000']['hair_color']['reason'] = 'changed validation copy'
    assert result['raw_data'] == before['data']
    assert raw == before


def det(box, area, confidence=.9):
    return {'bbox': box, 'area_ratio': area, 'confidence': confidence}


def test_all_person_boxes_containing_primary_face_are_excluded():
    result = {'faces': [det([20, 10, 30, 20], .01)],
              'persons': [det([10, 0, 50, 100], .4), det([15, 5, 35, 25], .04)],
              'size': [100, 100]}
    # The tight containing box is primary; the other box has IoU .1 with it.
    assert v3.extra_candidates(result, {}) == []


@pytest.mark.parametrize('overlap_box', [[20, 0, 80, 100], [19, 0, 79, 100]])
def test_person_box_with_at_least_half_iou_is_excluded(overlap_box):
    result = {'faces': [det([10, 10, 20, 20], .01)],
              'persons': [det([0, 0, 60, 100], .6), det(overlap_box, .6)],
              'size': [100, 100]}
    assert v3.extra_candidates(result, {}) == []


def test_person_box_below_half_iou_is_retained():
    result = {'faces': [det([10, 10, 20, 20], .01)],
              'persons': [det([0, 0, 60, 100], .6), det([21, 0, 81, 100], .6)],
              'size': [100, 100]}
    candidates = v3.extra_candidates(result, {})
    assert [(c['kind'], c['index']) for c in candidates] == [('person', 1)]


def test_small_person_survives_filtering_and_its_face_is_not_repeated():
    result = {'faces': [det([20, 10, 30, 20], .01), det([85, 5, 89, 9], .0016)],
              'persons': [det([10, 0, 50, 100], .4), det([83, 3, 93, 25], .022)],
              'size': [100, 100]}
    candidates = v3.extra_candidates(result, {})
    assert [(c['kind'], c['index']) for c in candidates] == [('person', 1)]
    assert candidates[0]['below_count_threshold'] is True


def test_unmatched_small_face_rule_is_preserved():
    result = {'faces': [det([20, 10, 30, 20], .01), det([85, 5, 89, 9], .0016)],
              'persons': [det([10, 0, 50, 100], .4)], 'size': [100, 100]}
    candidates = v3.extra_candidates(result, {})
    assert [(c['kind'], c['index']) for c in candidates] == [('face', 1)]
    assert candidates[0]['below_count_threshold'] is True


def test_without_faces_person_iou_filter_uses_largest_person():
    result = {'faces': [], 'persons': [det([0, 0, 60, 100], .6), det([20, 0, 80, 100], .59)],
              'size': [100, 100]}
    assert v3.extra_candidates(result, {}) == []
