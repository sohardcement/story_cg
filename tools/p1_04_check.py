"""Read-only verification of a delivered story job and its remote generation history."""
import argparse
import hashlib
import json
from pathlib import Path
import httpx
from storycg.assembly import assemble_prompt
from storycg.planning import validate_storyboard

parser = argparse.ArgumentParser()
parser.add_argument('job', type=Path)
parser.add_argument('preview', type=Path)
parser.add_argument('--baseline-job', type=Path)
parser.add_argument('--baseline-preview', type=Path)
args = parser.parse_args()
p = args.job
state = json.loads((p / 'state.json').read_text())
board = json.loads((p / 'storyboard.json').read_text())
identity = json.loads((p / 'identity.json').read_text())
manifest = json.loads((p / 'manifest.json').read_text())
validate_storyboard(board, len(board['shots']), identity)
assert len(manifest['images']) == len(board['shots'])
assert state['stats']['submission_attempts'] <= state['config']['budget']['max_images_per_job']
with httpx.Client(base_url=state['config']['comfyui_url'], timeout=180, trust_env=False) as client:
    for attempt in state['attempts']:
        if attempt['status'] != 'completed':
            raise AssertionError(f"incomplete attempt: {attempt['shot_id']}")
        path = p / attempt['image']['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == attempt['image']['sha256']
        response = client.get('/history/' + attempt['prompt_id'])
        response.raise_for_status()
        assert response.json()[attempt['prompt_id']]['status']['status_str'] == 'success'
        print(attempt['shot_id'], attempt['attempt_number'], attempt['prompt_id'], 'success')
for entry, item in zip(board['shots'], manifest['images']):
    latest = [a for a in state['attempts'] if a['shot_id'] == item['shot_id']][-1]
    assert item['prompt_id'] == latest['prompt_id']
    parts = assemble_prompt(identity, entry, props=board['props'], config=state['config'])
    assert item['identity_block'] == identity['identity_block'].replace('_', ' ')
    assert item['positive_prompt'] == state['config']['positive_prefix'] + ', ' + parts['positive_tags']
    assert '_' not in item['positive_prompt'] and 'protagonist' not in item['positive_prompt']
    for tag in entry['scene']['negative_tags']:
        assert tag.replace('_', ' ') in item['negative_prompt'].split(', ')
    for ref in entry['shot']['key_props']:
        prop = next(x for x in board['props'] if x['id'] == ref['prop_id'])
        assert ', '.join(t.replace('_', ' ') for t in prop['description_tags']) in item['positive_prompt']
assert args.preview.read_bytes() == (p / 'contact_sheet.jpg').read_bytes()
if args.baseline_job and args.baseline_preview:
    assert args.baseline_preview.read_bytes() == (args.baseline_job / 'contact_sheet.jpg').read_bytes()
log = json.loads((p / 'plan_log.json').read_text())
assert len(log['calls']) + manifest['usage'].get('review_calls', 0) <= 25
if (p / 'review_log.jsonl').exists():
    rows = [json.loads(x) for x in (p / 'review_log.jsonl').read_text().splitlines()]
    blind = next(x['request'] for x in rows if x['event'] == 'request' and x.get('purpose') == 'narrative_blind')
    assert all(c['type'] == 'image_url' for c in blind['messages'][0]['content'])
print('PNG hashes/history, latest prompts, identity conversion, prop descriptions, scene exclusions, preview preservation, call budget and image-only blind request verified.')
