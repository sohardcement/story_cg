"""V2-37: prompt wording A/B experiment. Generation only: no review, no selection, no vision or local model.

A = assemble_prompt output as the formal flow builds it.
B = 1girl + character tag + work tag, then the shot block (framing, camera, action, expression),
    then at most 2 appearance tags (locked hair colour, locked eye colour), then at most 2 outfit tags
    (highest-frequency upper and lower garment of the standard outfit), then the scene block.
Both use the same seed pair, the same IPAdapter face reference (strength 0.4, taken from the job's own
attempt record), the same full negative prompt and the same job config snapshot.

Stages (run from the v2 worktree):
  PYTHONPATH=$PWD/src .venv/bin/python tools/v2_37_ab.py prepare   # identity replay + prompts, no image
  PYTHONPATH=$PWD/src .venv/bin/python tools/v2_37_ab.py run       # 24 submissions, one at a time
Read-only inputs: jobs/<job>/identity.json, jobs/<job>/state.json (config and reference only).
Writes: reports/assets/V2-37/ only.
"""
import copy
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

import httpx

from storycg.assembly import assemble_prompt, full_negative_prompt
from storycg.comfy import ComfyClient
from storycg.identity import BASE_URL, USER_AGENT, resolve_identity
from storycg.runner import run_candidate
from storycg.state import load_state
from storycg.tags import colour_words

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports' / 'assets' / 'V2-37'
IMAGES = OUT / 'images'
JOBS = OUT / 'jobs'
CACHE = ROOT / 'cache' / 'danbooru'
SEEDS = (20261201, 20261202)
SCENE = {'location': 'window seat, indoors', 'time': 'daytime', 'weather': 'clear', 'lighting': 'daylight',
         'environment_change': 'none', 'tags': ['indoors', 'daytime', 'window'],
         'negative_tags': ['outdoors', 'street', 'night', 'rain']}
# (number, framing, camera_angle, pose/action, expression)
SHOTS = [(1, 'medium', 'side', 'hugging_own_legs', 'crying'),
         (2, 'closeup', 'front', 'wiping_tears', 'tears'),
         (3, 'medium', 'front', 'stretching', 'smile')]
CHARACTERS = [
    {'key': 'frieren', 'job': 'cg-d86de5dc9b1a', 'job_id': 'v237-frieren'},
    {'key': 'miku', 'job': 'cg-778717bdaa05', 'job_id': 'v237-miku'},
]
# Hair and eye colour words. colour_words() has no 'aqua'; Miku's locked hair colour is aqua, so it is added here.
HAIR_EYE_COLOURS = sorted(set(colour_words()) | {'aqua'}, key=len, reverse=True)
UPPER = re.compile(r'(?:^|_)(?:shirt|jacket|blouse|sweater|top|coat|vest|hoodie|cardigan)$')
LOWER = re.compile(r'(?:^|_)(?:skirt|pants|shorts|trousers|jeans)$')


def shot_dict(num, framing, camera, action, expr):
    return {'narrative_role': 'v2-37 experiment', 'expression': expr, 'spatial_relation': '',
            'change_from_previous': '', 'composition_reason': '', 'prop_tags': [], 'camera_angle': camera,
            'framing': framing, 'subject_visibility': 'clear', 'reflection': False, 'other_people': [],
            'hand_owners': [], 'unchanged_person_reason': '', 'pose': action, 'protagonist_instances': 1,
            'action': action, 'description': action, 'tags': [action, expr], 'key_props': []}


def job_state(job):
    return json.loads((ROOT / 'jobs' / job / 'state.json').read_text(encoding='utf-8'))


def replay_identity(char):
    stored = json.loads((ROOT / 'jobs' / char['job'] / 'identity.json').read_text(encoding='utf-8'))
    with httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False,
                      headers={'User-Agent': USER_AGENT}) as client:
        return resolve_identity(stored['input_name'], CACHE, client=client,
                                character_tag=stored['character_tag'])


def locked_colour_tag(fresh, suffix):
    for item in fresh.get('appearance_tags', []):
        tag = item['tag']
        for colour in HAIR_EYE_COLOURS:
            if tag == f'{colour}_{suffix}':
                return tag
    return None


def standard_garment(fresh, pattern):
    """Highest-frequency garment of the standard outfit matching pattern; None when none matches."""
    candidates = [t for t in fresh.get('default_outfit_tags', []) if pattern.search(t['tag'])]
    if not candidates:
        return None
    return max(candidates, key=lambda t: (t.get('frequency') or 0.0))['tag']


def spaced(tags):
    return [t.replace('_', ' ') for t in tags if t]


def build_b(fresh, parts, character_tag):
    identity_tags = fresh['identity_block'].split(', ')
    assert identity_tags[0] == '1girl' and identity_tags[1] == character_tag, identity_tags[:2]
    core = spaced(identity_tags[:3])
    shot = parts['shot_block'].split(', ') if parts['shot_block'] else []
    appearance = spaced([locked_colour_tag(fresh, 'hair'), locked_colour_tag(fresh, 'eyes')])
    outfit = spaced([standard_garment(fresh, UPPER), standard_garment(fresh, LOWER)])
    scene = parts['scene_block'].split(', ') if parts['scene_block'] else []
    parts_b = [*core, *shot, *appearance, *outfit, *scene]
    seen, ordered = set(), []
    for tag in parts_b:
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ', '.join(ordered), {'core': core, 'shot': shot, 'appearance': appearance, 'outfit': outfit,
                                'scene': scene}


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    replay, prompts = [], []
    for char in CHARACTERS:
        fresh = replay_identity(char)
        state = job_state(char['job'])
        config = state['config']
        reference = state['attempts'][0]['reference']
        assert reference['mechanism'] == 'ipadapter' and reference['strength'] == 0.4, reference
        replay.append({'character': char['key'], 'source_job': char['job'], 'status': fresh.get('status'),
                       'character_tag': fresh.get('character_tag'), 'work_tag': fresh.get('work_tag'),
                       'appearance_locked': [(t['tag'], t.get('frequency')) for t in fresh.get('appearance_tags', [])],
                       'outfit_locked': [(t['tag'], t.get('frequency')) for t in fresh.get('default_outfit_tags', [])],
                       'identity_block': fresh.get('identity_block'), 'outfit_block': fresh.get('outfit_block'),
                       'b_hair_tag': locked_colour_tag(fresh, 'hair'), 'b_eye_tag': locked_colour_tag(fresh, 'eyes'),
                       'b_upper_tag': standard_garment(fresh, UPPER), 'b_lower_tag': standard_garment(fresh, LOWER),
                       'reference': {k: reference.get(k) for k in ('mechanism', 'strength', 'uploaded_image',
                                                                  'portrait_sha256', 'source_task')}})
        for number, framing, camera, action, expr in SHOTS:
            entry = {'number': number, 'scene': SCENE,
                     'outfit': {'clothing_state': 'standard outfit',
                                'tags': [t['tag'] for t in fresh['default_outfit_tags']], 'change_at_shot': None},
                     'shot': shot_dict(number, framing, camera, action, expr)}
            parts = assemble_prompt(fresh, entry, config=config)
            negative = full_negative_prompt(config, parts)
            pos_b, detail_b = build_b(fresh, parts, fresh['character_tag'])
            pos_a = parts['positive_tags']
            for seed_index, seed in enumerate(SEEDS):
                for style_index, (style, positive) in enumerate((('A', pos_a), ('B', pos_b))):
                    prompts.append({
                        'character': char['key'], 'job_id': char['job_id'], 'shot': number, 'framing': framing,
                        'camera_angle': camera, 'action': action, 'expression': expr,
                        'shot_id': f'shot{number:02d}', 'seed': seed, 'style': style,
                        'candidate': style_index * 2 + seed_index + 1,
                        'positive': positive, 'negative': negative,
                        'positive_tag_count': len(positive.split(', ')),
                        'positive_char_count': len(positive), 'negative_tag_count': len(negative.split(', ')),
                        'negative_char_count': len(negative), 'token_count': '未统计',
                        'framing_for_run': framing, 'dropped_scene_tags': parts['dropped_scene_tags'],
                        'indoor_precipitation_negative': parts['indoor_precipitation_negative'],
                        'parts_A': {k: parts[k] for k in ('identity_block', 'outfit_block', 'scene_block', 'shot_block')},
                        'parts_B': detail_b,
                    })
    (OUT / 'identity_replay.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'prompts.json').write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'prepared {len(prompts)} prompts')
    return prompts


def wait_queue_idle(client, limit=3600):
    deadline = time.monotonic() + limit
    while True:
        queue = client.get_queue()
        if not queue.get('queue_running') and not queue.get('queue_pending'):
            return
        if time.monotonic() > deadline:
            raise RuntimeError('ComfyUI queue did not drain')
        time.sleep(5)


def load_manifest():
    path = OUT / 'manifest.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'experiment': 'V2-37', 'items': []}


def save_manifest(manifest):
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def generate(job_dir, job_id, item, config, reference, client, candidate):
    shot_config = copy.deepcopy(config)
    shot_config['negative_prompt'] = item['negative']
    shot_config['auto_retry_failed'] = False
    started = time.monotonic()
    wait_queue_idle(client)
    run_candidate(job_dir, item['shot_id'], candidate, item['seed'], item['positive'], item['framing_for_run'],
                  shot_config, client, negative_prompt=item['negative'], generation_reference=reference,
                  dropped_scene_tags=item['dropped_scene_tags'],
                  indoor_negative=item['indoor_precipitation_negative'])
    elapsed = time.monotonic() - started
    state = load_state(job_dir)
    attempt = [a for a in state['attempts'] if a['shot_id'] == item['shot_id'] and a.get('candidate') == candidate][-1]
    return attempt, elapsed


def run():
    prompts = json.loads((OUT / 'prompts.json').read_text(encoding='utf-8'))
    manifest = load_manifest()
    done = {(i['character'], i['shot'], i['seed'], i['style']) for i in manifest['items'] if i['status'] == 'completed'}
    configs = {c['key']: job_state(c['job'])['config'] for c in CHARACTERS}
    references = {c['key']: job_state(c['job'])['attempts'][0]['reference'] for c in CHARACTERS}
    job_ids = {c['key']: c['job_id'] for c in CHARACTERS}
    config0 = configs[CHARACTERS[0]['key']]
    client = ComfyClient(config0['comfyui_url'], config0['http_timeout_seconds'])
    IMAGES.mkdir(parents=True, exist_ok=True)
    try:
        for item in prompts:
            key = (item['character'], item['shot'], item['seed'], item['style'])
            if key in done:
                continue
            char = item['character']
            job_dir = JOBS / job_ids[char]
            record = {k: item[k] for k in ('character', 'job_id', 'shot', 'framing', 'camera_angle', 'action',
                                           'expression', 'seed', 'style', 'candidate', 'positive', 'negative',
                                           'positive_tag_count', 'positive_char_count', 'negative_tag_count',
                                           'negative_char_count', 'token_count', 'parts_A', 'parts_B')}
            attempts_log = []
            final = None
            for candidate in (item['candidate'], item['candidate'] + 10):  # second try: same seed, new slot id
                try:
                    attempt, elapsed = generate(job_dir, job_ids[char], item, configs[char], references[char],
                                                client, candidate)
                except Exception as exc:  # recorded; one re-submit with the same seed is allowed
                    attempts_log.append({'candidate_slot': candidate, 'status': 'exception',
                                         'error': f'{type(exc).__name__}: {exc}'})
                    continue
                attempts_log.append({'candidate_slot': candidate, 'status': attempt['status'],
                                     'prompt_id': attempt.get('prompt_id'), 'elapsed_seconds': round(elapsed, 1)})
                if attempt['status'] == 'completed':
                    final = (attempt, elapsed, candidate)
                    break
            record['attempts'] = attempts_log
            if final is None:
                record['status'] = 'skipped'
            else:
                attempt, elapsed, candidate = final
                source = job_dir / f"{Path(attempt['filename_prefix']).name}.png"
                name = (f"{char}_s{item['shot']}-{item['framing']}-{item['camera_angle']}-{item['expression']}"
                        f"_seed{item['seed']}_{item['style']}.png")
                target = IMAGES / name
                shutil.copyfile(source, target)
                record.update({'status': 'completed', 'image_path': str(target.relative_to(ROOT)),
                               'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                               'prompt_id': attempt.get('prompt_id'), 'filename_prefix': attempt['filename_prefix'],
                               'elapsed_seconds': round(elapsed, 1), 'submitted_candidate': candidate,
                               'reference': {k: attempt['reference'].get(k) for k in
                                             ('mechanism', 'strength', 'uploaded_image')}})
            manifest['items'] = [i for i in manifest['items'] if (i['character'], i['shot'], i['seed'], i['style']) != key]
            manifest['items'].append(record)
            manifest['items'].sort(key=lambda i: (i['character'], i['shot'], i['seed'], i['style']))
            save_manifest(manifest)
            print(record['character'], record['shot'], record['seed'], record['style'], record['status'], flush=True)
    finally:
        client.close()
    completed = [i for i in manifest['items'] if i['status'] == 'completed']
    print(f'completed {len(completed)} of {len(prompts)}')


if __name__ == '__main__':
    stage = sys.argv[1] if len(sys.argv) > 1 else ''
    if stage == 'prepare':
        prepare()
    elif stage == 'run':
        run()
    else:
        raise SystemExit(__doc__)
