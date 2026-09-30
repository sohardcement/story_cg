"""Reproducible P7 CLI examples using OFFLINE service fixtures; never generation evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import uuid

from PIL import Image
from storycg import agent
from storycg.__main__ import main
from storycg.state import save_json_atomic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tests'))
from test_p1_04 import board

ROOT = Path(__file__).resolve().parents[1]/'reports/assets'/('P7-01-offline-'+uuid.uuid4().hex[:8])
ROOT.mkdir(parents=True)
outputs = []


def call(argv, label):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    outputs.append({'label': label, 'offline': True, 'command': ['.venv/bin/python', '-m', 'storycg', *argv],
                    'exit_code': code, 'output': json.loads(out.getvalue())})


def fixture_plan(root, config):
    value = board()
    for entry in value['shots']:
        entry['shot'].update(subject_visibility='clear', other_people=[], hand_owners=['subject'])
        for prop in entry['shot']['key_props']: prop['detached'] = False
    save_json_atomic(root/'storyboard.json', value)
    return value


def fixture_identity(root):
    identity = {'status': 'locked', 'input_name': '示例主角', 'character_tag': 'sample', 'identity_block': '1girl, sample'}
    save_json_atomic(root/'identity.json', identity)
    return identity


def fixture_llm(root, config, purpose, content, schema):
    return {'named_characters': ['示例主角', '示例配角'], 'conflict': ''} if purpose == 'intake' else {'summaries': ['主角注意到物品的变化。']*4}


def fixture_generate(root, config):
    # Explicit synthetic test files, inherited excludes them from actual service usage.
    attempts = []
    for n in range(1, 5):
        path = root/f'OFFLINE-fixture-{n}.png'
        Image.new('RGB', (160, 120), (40*n, 80, 100)).save(path)
        attempts.append({'shot_id': f'shot{n:02d}', 'attempt_number': 1, 'status': 'completed',
                         'inherited': True, 'post_count': 0, 'image': {'path': path.name}})
    save_json_atomic(root/'state.json', {'job_id': root.name, 'attempts': attempts})
    save_json_atomic(root/'manifest.json', {'offline_fixture': True, 'images': [], 'models': {'status': 'offline fixture'}})
    (root/'result.md').write_text('# 离线夹具\n\n合成色块仅验证交付格式，不是出图结果，不存在真实审核。\n')
    agent.deliver(root)
    agent.update(root, phase='completed')


with patch.object(agent, 'resolve_job', fixture_identity), patch.object(agent, 'plan_job', fixture_plan), patch.object(agent, 'runtime_json', fixture_llm), patch.object(agent, 'generate', fixture_generate):
    root = ROOT/'board'
    call(['make', str(root), '--character', '示例主角', '--story', '示例主角与示例配角发现物品发生变化。', '--storyboard-first', '--foreground'], 'make storyboard-first + multi-name notice')
    call(['status', str(root)], 'status before approval')
    call(['resume', str(root), '--foreground'], 'resume must not bypass approval')
    def edited(*args, **kwargs):
        value = fixture_plan(root, {}); value['shots'][-1]['shot']['action'] = 'return object'
        return value
    with patch.object(agent, 'plan_story', edited):
        call(['approve', str(root), '--edit', '最后归还物品。', '--foreground'], 'approve edit with offline planner fixture')
    call(['status', str(root)], 'status completed fixture')
    call(['deliver', str(root)], 'deliver synthetic fixture only')
    with patch.object(agent, 'launch', lambda root: None):
        call(['make', str(ROOT/'background'), '--character', '示例主角', '--story', '剧情示例'], 'make immediate return (offline launch fixture)')
    with patch.object(agent, 'resolve_job', lambda root: {'status': 'needs_clarification', 'candidates': [{'tag': 'candidate_a'}, {'tag': 'candidate_b'}]}):
        call(['make', str(ROOT/'ambiguous'), '--character', '不明确的角色', '--story', '剧情示例', '--storyboard-first', '--foreground'], 'identity question fixture')
    with patch.object(agent, 'runtime_json', lambda *a: {'named_characters': [], 'conflict': '同一结局被要求发生与不发生。'}):
        call(['make', str(ROOT/'conflict'), '--character', '示例主角', '--story', '互斥要求示例', '--storyboard-first', '--foreground'], 'conflict question fixture')
    with patch.object(agent, 'resolve_job', side_effect=agent.ResourceUnavailable('离线夹具模拟服务不可达')):
        call(['make', str(ROOT/'resource'), '--character', '示例主角', '--story', '剧情示例', '--storyboard-first', '--foreground'], 'resource question fixture')
path = ROOT/'examples.json'
save_json_atomic(path, {'offline_fixture_only': True, 'examples': outputs})
print(path)
