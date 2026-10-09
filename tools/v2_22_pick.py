"""V2-22: side-by-side pick, forward and reverse order, offline experiment.

Reads only jobs/cg-8092d80fd20b/candidates.json and storyboard.json (read-only). Writes only under
reports/assets/V2-22/. Makes at most 8 vision requests in total (6 planned, 2 for retries).
Run with PYTHONPATH=$PWD/src. Does not read verify/.
"""
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from storycg.review import VisionClient, messages, object_schema, TEXT
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / 'jobs/cg-8092d80fd20b'
OUT = ROOT / 'reports/assets/V2-22'
CONFIG = json.loads((ROOT / 'config/default.json').read_text(encoding='utf-8'))
MAX_CALLS = 8
LETTERS = 'ABCDEFGH'
HEIGHT = 1024
LABEL_H = 110
GAP = 16

PROMPT_TEMPLATE = (
    'You see {n} images side by side, labelled {labels} from left to right. Each image is a candidate picture '
    'of the same character. Requirement for this one shot only:\n'
    '- framing: {framing}\n'
    '- spatial relation: {spatial_relation}\n'
    '- pose / action: {pose}\n'
    '- expression: {expression}\n'
    '- narrative role of this shot: {narrative_role}\n'
    'Question: which image makes the requested action and expression most readable at a glance? '
    'Judge only the visible character\'s action and expression against this requirement. '
    'For every image, say whether the requested action is visible and whether the requested expression '
    'is visible, each as yes, partly or no. Answer with JSON only, following the schema: '
    '"best" is one of the labels; "reason" is one short sentence; "per_image" has one entry per label.'
)


def schema_for(labels):
    per_image = {'type': 'array', 'minItems': len(labels), 'maxItems': len(labels), 'items': object_schema({
        'label': {'enum': list(labels)},
        'action_visible': {'enum': ['yes', 'partly', 'no']},
        'expression_visible': {'enum': ['yes', 'partly', 'no']}})}
    return object_schema({'best': {'enum': list(labels)}, 'reason': TEXT, 'per_image': per_image})


def composite(paths, labels, out_path):
    images = []
    for path in paths:
        with Image.open(path) as source:
            image = source.convert('RGB')
        scale = HEIGHT / image.height
        images.append(image.resize((round(image.width * scale), HEIGHT), Image.LANCZOS))
    width = sum(i.width for i in images) + GAP * (len(images) + 1)
    sheet = Image.new('RGB', (width, HEIGHT + LABEL_H + 2 * GAP), 'white')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=72)
    x = GAP
    for image, label in zip(images, labels):
        sheet.paste(image, (x, GAP))
        draw.text((x + image.width // 2 - 24, GAP + HEIGHT + 12), label, font=font, fill='black')
        x += image.width + GAP
    sheet.save(out_path, 'PNG')
    return out_path


def shot_fields(board, number):
    shot = next(e['shot'] for e in board['shots'] if e['number'] == number)
    return {k: shot.get(k, '') for k in ('framing', 'spatial_relation', 'pose', 'expression', 'narrative_role')}


def main():
    store = json.loads((JOB / 'candidates.json').read_text(encoding='utf-8'))
    board = json.loads((JOB / 'storyboard.json').read_text(encoding='utf-8'))
    OUT.mkdir(parents=True, exist_ok=True)
    calls_path = OUT / 'calls.jsonl'
    calls_path.write_text('', encoding='utf-8')
    client = VisionClient(OUT, CONFIG['model_service_url'], max_calls=MAX_CALLS, max_seconds=2700)
    summary = []
    try:
        for key in sorted(store, key=int):
            number = int(key)
            pool = sorted((c for c in store[key]['candidates'] if c['status'] == 'completed' and not c['screen']),
                          key=lambda c: c['candidate'])
            if len(pool) < 2:
                summary.append({'shot': number, 'pool': [c['candidate'] for c in pool], 'skipped': 'fewer than 2 passing'})
                continue
            fields = shot_fields(board, number)
            result = {'shot': number, 'pool': [c['candidate'] for c in pool], 'orders': {}}
            for order in ('forward', 'reverse'):
                ordered = pool if order == 'forward' else list(reversed(pool))
                labels = list(LETTERS[:len(ordered)])
                letter_to_candidate = {l: c['candidate'] for l, c in zip(labels, ordered)}
                paths = [JOB / c['image']['path'] for c in ordered]
                sheet = composite(paths, labels, OUT / f'shot{number:02d}_{order}.png')
                sha = hashlib.sha256(sheet.read_bytes()).hexdigest()
                text = PROMPT_TEMPLATE.format(n=len(labels), labels=', '.join(labels), **fields)
                schema = schema_for(labels)
                outcome = None
                for attempt in (1, 2):
                    response = client.call(f'v2_22_shot{number}_{order}_try{attempt}', messages(text, [sheet]), schema, [sheet])
                    record = {'shot': number, 'order': order, 'attempt': attempt, 'candidate_pool': result['pool'],
                              'letter_to_candidate': letter_to_candidate, 'composite': str(sheet.relative_to(OUT)),
                              'composite_sha256': sha, 'prompt': text, 'call_id': response.get('call_id'),
                              'status': response.get('status'), 'error': response.get('error'),
                              'data': response.get('data'), 'elapsed_seconds': response.get('elapsed_seconds')}
                    if response.get('data'):
                        best = response['data']['best']
                        record['best_candidate'] = letter_to_candidate[best]
                    with calls_path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    if response.get('status') == 'pass':
                        outcome = record
                        break
                if outcome is None:
                    result['orders'][order] = {'error': record.get('error'), 'best_candidate': None}
                else:
                    result['orders'][order] = {'best_letter': outcome['data']['best'],
                                               'best_candidate': outcome['best_candidate'],
                                               'reason': outcome['data']['reason'],
                                               'per_image': outcome['data']['per_image']}
            fwd = result['orders']['forward'].get('best_candidate')
            rev = result['orders']['reverse'].get('best_candidate')
            result['consistent'] = (fwd is not None and fwd == rev)
            summary.append(result)
    finally:
        client.close()
    save_json_atomic(OUT / 'summary.json', {'calls_made': client.calls, 'max_calls': MAX_CALLS, 'shots': summary})
    for item in summary:
        print(json.dumps(item, ensure_ascii=False))
    print('calls made:', client.calls, 'of', MAX_CALLS)


if __name__ == '__main__':
    sys.exit(main())
