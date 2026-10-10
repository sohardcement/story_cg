"""Offline delivery-note tests; fixtures are not calibration evidence."""

import copy
from datetime import datetime, timezone

from PIL import Image
import pytest

from storycg import agent, cycle, review
from storycg.state import save_json_atomic


NOTE = '候选审核说明；检测器不可见的人物仍可能漏检。'


def config_file(root, *, note=NOTE):
    path = root / 'reliability.json'
    save_json_atomic(path, {'items': {
        'extra_protagonist': {'status': 'reliable', 'note': note},
        'hair_color': {'status': 'reliable'},
        'action': {'status': 'unverified', 'note': ''},
    }})
    return {'review_reliability_file': str(path)}


def report_without_items():
    return {'conclusion': '未通过', 'reference_note': '离线测试参考',
            'layer_conclusions': {}, 'single': [], 'consistency': [], 'faces': [],
            'narrative_blind': {'status': 'fail', 'error': 'offline fixture'},
            'narrative_compare': {'status': 'fail', 'error': 'offline fixture'}, 'usage': {}}


@pytest.mark.parametrize('status', ['reliable', 'unreliable', 'unverified'])
def test_annotation_keeps_configured_note_without_changing_grade(status):
    result = {'status': 'pass', 'data': {
        'extra_protagonist': {'status': 'pass', 'reason': '离线测试记录'}}}
    annotated = review.annotate_reliability(result, {
        'extra_protagonist': {'status': status, 'note': NOTE}})
    item = annotated['data']['extra_protagonist']
    assert NOTE in item['reliability_note']
    assert item['status'] == 'pass'
    assert item['reliability'] == status
    assert item['verified'] is (status == 'reliable')
    assert ('该项未经可靠审核' in item['reliability_note']) is (status != 'reliable')


def test_annotation_uses_configured_note_for_mapped_item():
    result = {'status': 'pass', 'data': {
        'outfit_upper': {'status': 'minor', 'reason': '离线测试记录'}}}
    item = review.annotate_reliability(result, {
        'outfit': {'status': 'reliable', 'note': NOTE}})['data']['outfit_upper']
    assert item['reliability_note'] == NOTE
    assert item['status'] == 'minor'


@pytest.mark.parametrize('source', ['report', 'config', 'report_missing_note'])
def test_review_markdown_shows_note_without_item_records(tmp_path, source):
    config = config_file(tmp_path)
    report = report_without_items()
    if source == 'report':
        report['reliability'] = {'extra_protagonist': {
            'status': 'reliable', 'note': NOTE}}
        config_file(tmp_path, note='当前配置不同说明')
    elif source == 'report_missing_note':
        report['reliability'] = {'extra_protagonist': {'status': 'unreliable'}}
    before = copy.deepcopy(report)
    review.write_review_md(tmp_path, report, config)
    text = (tmp_path / 'review.md').read_text()
    assert '- extra_protagonist: ' + NOTE in text
    assert '当前配置不同说明' not in text
    assert 'hair_color:' not in text
    assert 'action:' not in text
    assert '100%' not in text
    assert report == before


@pytest.mark.parametrize('source', ['report', 'config'])
def test_cycle_result_shows_note_without_item_records(tmp_path, source):
    root = tmp_path / 'jobs' / 'offline'
    root.mkdir(parents=True)
    config = config_file(root)
    save_json_atomic(root / 'job.json', {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'job_id': 'offline', 'character': 'fixture', 'story': 'offline fixture'})
    save_json_atomic(root / 'storyboard.json', {'shots': [], 'props': []})
    save_json_atomic(root / 'identity.json', {})
    save_json_atomic(root / 'state.json', {'attempts': []})
    save_json_atomic(root / 'models_snapshot.json', {'status': 'offline fixture'})
    report = report_without_items()
    if source == 'report':
        report['reliability'] = {'extra_protagonist': {'note': NOTE}}
        config_file(root, note='当前配置不同说明')
    from storycg import delivery
    delivery.deliver(root, {'shots': {}, 'final_review': report}, config, None)
    text = (root / 'result.md').read_text()
    assert '- extra_protagonist: ' + NOTE in text
    assert '当前配置不同说明' not in text
    assert 'hair_color:' not in text
    assert '100%' not in text


@pytest.mark.parametrize('with_final_report', [False, True])
def test_agent_result_keeps_note_after_final_render_and_repeat(tmp_path, with_final_report):
    config = config_file(tmp_path)
    # Synthetic pixels allow the real offline delivery path; no generated image or API.
    Image.new('RGB', (16, 16), 'white').save(tmp_path / 'fixture.png')
    save_json_atomic(tmp_path / 'job.json', {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'job_id': 'offline', 'character': 'fixture', 'story': 'offline fixture',
        'generation_config': config})
    save_json_atomic(tmp_path / 'storyboard.json', {
        'shots': [{'number': 1}], 'prop_dependent': False})
    save_json_atomic(tmp_path / 'state.json', {'attempts': [{
        'shot_id': 'shot01', 'attempt_number': 1, 'status': 'completed',
        'image': {'path': 'fixture.png', 'sha256': 'offline fixture'}}]})
    saved = {'shots': {}}
    if with_final_report:
        saved['final_review'] = report_without_items()
    save_json_atomic(tmp_path / 'cycle.json', saved)
    (tmp_path / 'result.md').write_text('# 结果说明\n\n已有说明。\n')
    agent.deliver(tmp_path)
    agent.deliver(tmp_path)
    text = (tmp_path / 'result.md').read_text()
    assert text.count('- extra_protagonist: ' + NOTE) == 1
    assert 'hair_color:' not in text
    assert '100%' not in text
    if not with_final_report:
        assert '已有说明。' in text
