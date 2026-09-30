"""Fail-closed three-layer review of existing images; never generates images."""

import base64
import copy
import hashlib
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from PIL import Image, ImageOps

from storycg.state import save_json_atomic

TEXT = {'type': 'string', 'minLength': 1}
GRADE = {'type': 'object', 'additionalProperties': False,
         'required': ['status', 'reason'], 'properties': {
             'status': {'enum': ['pass', 'minor', 'fail']}, 'reason': TEXT}}
SINGLE_KEYS = ['body', 'hands', 'action', 'prop_contact', 'composition', 'style']
REFERENCE_NOTE = '参考=第1镜，非定妆图'


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


def grading_schema(keys):
    return object_schema({key: GRADE for key in keys})


def conclude(statuses):
    """No majority voting: any fail, missing or unknown status fails the group."""
    statuses = list(statuses)
    if not statuses or any(value not in ('pass', 'minor') for value in statuses):
        return '未通过'
    return '有瑕疵但可选' if 'minor' in statuses else '通过'


def image_part(path):
    # Re-encode pixels only; source metadata/prompt chunks never enter the request.
    import io
    buffer = io.BytesIO()
    with Image.open(path) as source:
        source.convert('RGB').save(buffer, format='PNG')
    return {'type': 'image_url', 'image_url': {
        'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')}}


def blind_messages(paths):
    """Accepts ONLY ordered paths, with no story/name/board/text-message inputs."""
    return [{'role': 'user', 'content': [image_part(path) for path in paths]}]


def messages(text, paths=()):
    return [{'role': 'user', 'content': [{'type': 'text', 'text': text},
                                        *[image_part(path) for path in paths]]}]


class VisionClient:
    def __init__(self, job_dir, url, *, model=None, client=None, max_calls=30,
                 max_seconds=3600, timeout=180):
        self.job_dir = Path(job_dir)
        self.url = url.rstrip('/')
        self.owned = client is None
        self.http = client or httpx.Client(base_url=self.url, timeout=timeout, trust_env=False)
        self.model = model
        self.session_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.max_seconds = max_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.call_seconds = 0.0

    def close(self):
        if self.owned:
            self.http.close()

    def log(self, record):
        self.job_dir.mkdir(parents=True, exist_ok=True)
        with (self.job_dir / 'review_log.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def discover(self):
        if self.model:
            return
        started = time.monotonic()
        record = {'event': 'model_discovery', 'session_id': self.session_id,
                  'request': {'method': 'GET', 'url': self.url + '/models'}}
        try:
            response = self.http.get(self.url + '/models')
            record['raw_response'] = response.text
            response.raise_for_status()
            available = [m for m in response.json()['data'] if m.get('loaded', True)
                         and 'vision' in m.get('capabilities', [])
                         and 'json_schema' in m.get('capabilities', [])]
            if not available:
                raise ValueError('no loaded vision/json_schema model')
            self.model = available[0]['id']
            record['model'] = self.model
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            record['elapsed_seconds'] = time.monotonic() - started
            self.log(record)

    def call(self, purpose, message_list, schema, paths=()):
        if self.calls >= self.max_calls or time.monotonic() - self.started >= self.max_seconds:
            return {'status': 'fail', 'error': 'review budget exhausted', 'purpose': purpose}
        call_id = f'{self.session_id}:{self.calls + 1}'
        started = time.monotonic()
        raw = None
        result = {'status': 'fail', 'purpose': purpose, 'call_id': call_id}
        try:
            self.discover()
            request = {'model': self.model, 'temperature': 0, 'max_tokens': 6500,
                       'messages': message_list, 'response_format': {
                           'type': 'json_schema', 'json_schema': {
                               'name': 'visual_review', 'strict': True, 'schema': schema}}}
            logged = copy.deepcopy(request)
            image_refs = iter(paths)
            for message in logged['messages']:
                if isinstance(message['content'], list):
                    for part in message['content']:
                        if part['type'] == 'image_url':
                            path = Path(next(image_refs))
                            part['image_url']['url'] = {'path': str(path.relative_to(self.job_dir)),
                                                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            self.calls += 1
            self.log({'event': 'request', 'session_id': self.session_id,
                      'call_id': call_id, 'purpose': purpose,
                      'at': datetime.now(timezone.utc).isoformat(),
                      'url': self.url + '/chat/completions', 'request': logged})
            remaining = self.max_seconds - (time.monotonic() - self.started)
            if remaining <= 0:
                raise TimeoutError('review time budget exhausted')
            response = self.http.post(self.url + '/chat/completions', json=request,
                                      timeout=min(180, remaining))
            raw = response.text
            response.raise_for_status()
            envelope = response.json()
            choice = envelope['choices'][0]
            if choice.get('finish_reason') not in (None, 'stop'):
                raise ValueError('incomplete or refused response')
            data = json.loads(choice['message']['content'])
            Draft202012Validator(schema).validate(data)
            result.update(status='pass', data=data)
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
        elapsed = time.monotonic() - started
        self.call_seconds += elapsed
        result['elapsed_seconds'] = elapsed
        self.log({'event': 'response', 'session_id': self.session_id, 'call_id': call_id,
                  'purpose': purpose, 'raw_response': raw, 'elapsed_seconds': elapsed,
                  'status': result['status'], 'error': result.get('error')})
        return result


def identity_checklist(identity, outfit):
    """Script-owned keys/expectations: never let the VLM invent identity traits."""
    groups = {key: {'expected': [], 'source': 'identity.json'} for key in
              ['hair_color', 'hairstyle', 'eye_color', 'signature_features',
               'hair_ornaments', 'accessories']}
    for item in identity.get('appearance_tags', []):
        tag = item['tag']
        if tag.endswith('_hair') and tag.split('_')[0] not in ('long', 'short', 'medium', 'very'):
            category = 'hair_color'
        elif tag.endswith('_eyes'):
            category = 'eye_color'
        elif any(word in tag for word in ('hair_ornament', 'hairclip', 'hairband', 'hair_bow', 'hair_ribbon')):
            category = 'hair_ornaments'
        elif any(word in tag for word in ('hair', 'bangs', 'tail', 'braid')):
            category = 'hairstyle'
        else:
            category = 'signature_features'
        groups[category]['expected'].append(item)
    for item in identity.get('default_outfit_tags', []):
        if any(word in item['tag'] for word in ('earring', 'necklace', 'bracelet', 'gem', 'accessory')):
            groups['accessories']['expected'].append(item)
        if any(word in item['tag'] for word in ('hair_ornament', 'hairclip', 'hairband', 'hair_bow', 'hair_ribbon')):
            groups['hair_ornaments']['expected'].append(item)
    for index, component in enumerate(outfit['clothing_state'].split(','), 1):
        if component.strip():
            groups[f'outfit_component_{index}'] = {
                'expected': component.strip(), 'source': 'storyboard.json:outfit.clothing_state'}
    groups['outfit_state'] = {'expected': outfit, 'source': 'storyboard.json:outfit'}
    return groups


def scene_checklist(current, previous):
    """Generate one scene check per task-supplied field without adding scene elements."""
    previous = previous or {}
    fields = dict.fromkeys([*current, *previous])
    return {f'scene_{field}': {
        'expected': {'current': current.get(field), 'previous': previous.get(field)},
        'source': f'storyboard.json:scene.{field}'} for field in fields}


def crop_face(source, bbox, output):
    if (len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                             or not math.isfinite(v) or not 0 <= v <= 1 for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError('invalid normalized face bbox')
    with Image.open(source) as image:
        w, h = image.size
        pixels = [math.floor(bbox[0] * w), math.floor(bbox[1] * h),
                  math.ceil(bbox[2] * w), math.ceil(bbox[3] * h)]
        crop = image.convert('RGB').crop(pixels)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        crop.save(output)
    return {'normalized_bbox': bbox, 'pixel_bbox': pixels,
            'path': str(output), 'sha256': hashlib.sha256(Path(output).read_bytes()).hexdigest()}


def face_pair(reference, target, output):
    canvas = Image.new('RGB', (640, 320), '#dddddd')
    for index, path in enumerate([reference, target]):
        with Image.open(path) as image:
            thumb = ImageOps.contain(image.convert('RGB'), (320, 320))
            canvas.paste(thumb, (index * 320 + (320 - thumb.width) // 2,
                                (320 - thumb.height) // 2))
    canvas.save(output)


def result_statuses(result):
    if result['status'] != 'pass':
        return ['fail']
    return [item['status'] for item in result['data'].values()]


BBOX_SCHEMA = object_schema({
    'found': {'type': 'boolean'},
    'bbox': {'type': 'array', 'minItems': 4, 'maxItems': 4,
             'items': {'type': 'number', 'minimum': 0, 'maximum': 1}}, 'reason': TEXT})
BLIND_SCHEMA = object_schema({key: TEXT for key in
                             ['where', 'event', 'action', 'change', 'ending', 'uncertainty']})
BLIND_SCHEMA['description'] = (
    'Read the images in their given order. Retell the story supported by the images: '
    'where, what happens, how the person acts, what changes, and the ending. '
    'Do not guess names. You may infer events, intentions, ownership and causal relationships '
    'from visual evidence. Distinguish observations from inferences, and explain the supporting '
    'visual evidence and confidence for each inference in uncertainty; identify ambiguous '
    'evidence and alternative interpretations rather than asserting unsupported events.')
BLIND_SCHEMA['properties']['uncertainty'] = {
    **TEXT, 'description': 'For inferred events, intentions, ownership and causal relationships, '
    'state the supporting visual evidence and confidence for each inference. Explain ambiguity '
    'and alternative interpretations; if no inference is made, say so.'}


def review_job(job_dir, config, *, client=None, max_calls=30, max_seconds=3600):
    job_dir = Path(job_dir).resolve()
    started = time.monotonic()
    manifest = json.loads((job_dir / 'manifest.json').read_text())
    board = json.loads((job_dir / 'storyboard.json').read_text())
    identity = json.loads((job_dir / 'identity.json').read_text())
    job = json.loads((job_dir / 'job.json').read_text())
    if len(manifest['images']) != len(board['shots']) or not manifest['images']:
        raise ValueError('review requires one manifest image per storyboard shot')
    vlm = client or VisionClient(job_dir, config['model_service_url'],
                                 max_calls=max_calls, max_seconds=max_seconds)
    artifacts = job_dir / 'review_artifacts' / vlm.session_id
    artifacts.mkdir(parents=True, exist_ok=True)
    paths = []
    inputs = []
    for number, (item, entry) in enumerate(zip(manifest['images'], board['shots']), 1):
        if entry['number'] != number or item['shot_id'] != f'shot{number:02d}':
            raise ValueError('manifest/storyboard shot order mismatch')
        source = (job_dir / item['path']).resolve()
        if not source.is_relative_to(job_dir):
            raise ValueError('source image outside job directory')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != item['sha256']:
            raise ValueError('source image hash mismatch')
        sanitized = artifacts / f'sequence_{number:02d}.png'
        with Image.open(source) as image:
            image.convert('RGB').save(sanitized)
        paths.append(sanitized)
        inputs.append({'shot': number, 'source': item['path'], 'sha256': digest,
                       'review_copy': str(sanitized.relative_to(job_dir)),
                       'review_copy_sha256': hashlib.sha256(sanitized.read_bytes()).hexdigest()})
    report = {'version': 1, 'session_id': vlm.session_id, 'reference_note': REFERENCE_NOTE,
              'inputs': inputs, 'single': [], 'faces': [], 'consistency': [],
              'rule': 'any fail/unknown/missing/error => 未通过; otherwise any minor => 有瑕疵但可选; all pass => 通过'}
    statuses = []
    try:
        for number, (path, entry) in enumerate(zip(paths, board['shots']), 1):
            result = vlm.call(f'single_{number:02d}', messages(
                'Audit the image, with concrete visible evidence for each grade. '
                'pass=clear and correct, minor=small flaw without blocking readability, '
                'fail=major defect, missing required event/prop, or cannot determine. '
                'Check body anatomy, hands, action versus expected shot, prop/object contact, '
                'composition/readability and coherent anime illustration style. Do not infer '
                'an absent action from the expected shot. Expected shot: ' +
                json.dumps(entry['shot'], ensure_ascii=False), [path]),
                grading_schema(SINGLE_KEYS), [path])
            report['single'].append({'shot': number, **result})
            statuses.extend(result_statuses(result))
        for number, path in enumerate(paths, 1):
            result = vlm.call(f'face_bbox_{number:02d}', messages(
                'Locate the main foreground protagonist face only, not a background person. '
                'Return normalized [left, top, right, bottom] relative to image width/height. '
                'Include forehead, eyes, nose, mouth, chin and visible ears with a small margin. '
                'If no identifiable face is visible return found=false, bbox=[0,0,0,0].', [path]),
                BBOX_SCHEMA, [path])
            face = {'shot': number, 'detection': result, 'status': 'fail'}
            if result['status'] == 'pass' and result['data']['found']:
                try:
                    output = artifacts / f'face_{number:02d}.png'
                    face.update(crop_face(path, result['data']['bbox'], output), status='pass')
                    face['path'] = str(output.relative_to(job_dir))
                except ValueError as exc:
                    face['error'] = str(exc)
            else:
                face['error'] = result.get('error', 'no face found')
            report['faces'].append(face)
            statuses.append(face['status'])
        for number, (path, entry, face) in enumerate(zip(paths, board['shots'], report['faces']), 1):
            checklist = identity_checklist(identity, entry['outfit'])
            previous_scene = board['shots'][number - 2]['scene'] if number > 1 else None
            checks = {**checklist, **scene_checklist(entry['scene'], previous_scene),
                      'scene_transition': {'expected': {'previous': previous_scene,
                                                       'current': entry['scene']}, 'source': 'storyboard.json:scene'}}
            if face['status'] != 'pass' or report['faces'][0]['status'] != 'pass':
                result = {'status': 'fail', 'error': 'face crop/reference unavailable'}
            else:
                pair = artifacts / f'face_pair_{number:02d}.png'
                face_pair(job_dir / report['faces'][0]['path'], job_dir / face['path'], pair)
                compare_paths = [pair, paths[0]]
                if number > 1:
                    compare_paths.append(paths[number - 2])
                compare_paths.append(path)
                result = vlm.call(f'consistency_{number:02d}', messages(
                    'Audit ONLY the script-generated checklist below, returning one grade per exact key. '
                    'First image is face comparison: LEFT reference, RIGHT current. '
                    + REFERENCE_NOTE + '. Remaining images: full reference, '
                    + ('previous full shot, ' if number > 1 else '') + 'current full shot. '
                    'For each identity feature, compare reference/current and sourced expectation separately '
                    'in the reason, not a global same-person judgment. Empty expectation means unsourced: '
                    'compare only visible reference features and say expectation not specified; never invent '
                    'a canonical feature. Clothing components come from current outfit state, allowing explicit '
                    'outfit changes only. For each script-generated scene field, check the specific '
                    'elements described by its current and previous values against the visible images. '
                    'Do not add expected scene elements absent from those values. Also assess visual '
                    'continuity, citing visible evidence; identical declared states do not excuse '
                    'unexplained visual changes. A null previous value means no prior expectation '
                    'was specified for that field. '
                    'Unobservable or ambiguous REQUIRED features => fail with reason. '
                    'pass=clear match; minor=small discrepancy; fail=major mismatch/unknown. Checklist: '
                    + json.dumps(checks, ensure_ascii=False), compare_paths),
                    grading_schema(checks), compare_paths)
                result['face_pair'] = str(pair.relative_to(job_dir))
            report['consistency'].append({'shot': number, 'checklist': checks, **result})
            statuses.extend(result_statuses(result))
        blind = vlm.call('narrative_blind', blind_messages(paths), BLIND_SCHEMA, paths)
        report['narrative_blind'] = blind
        statuses.append(blind['status'])
        if blind['status'] == 'pass':
            comparison = vlm.call('narrative_compare', messages(
                'Compare the independent image-only retelling to the user story. '
                'Grade whether location, key causal events, actions, changes and ending were actually '
                'read out. Do not supplement the retelling with intended or imagined events. '
                'pass=explicitly read out; minor=small ambiguity without loss of key event/ending; '
                'fail=missing/contradictory/unclear key event or ending. The user may not specify an '
                'ending: then judge whether the retelling contains a legible ending consistent with '
                'the user story; do not invent an expected outcome. User story: '
                + job['story'] + '\nImage-only retelling: '
                + json.dumps(blind['data'], ensure_ascii=False)),
                grading_schema(['location', 'key_events', 'actions', 'changes', 'ending']))
        else:
            comparison = {'status': 'fail', 'error': 'blind retelling failed; comparison skipped'}
        report['narrative_compare'] = comparison
        statuses.extend(result_statuses(comparison))
    finally:
        if client is None:
            vlm.close()
    report['conclusion'] = conclude(statuses)
    report['layer_conclusions'] = {
        'single': conclude(s for r in report['single'] for s in result_statuses(r)),
        'consistency': conclude([*(f['status'] for f in report['faces']),
                                *(s for r in report['consistency'] for s in result_statuses(r))]),
        'narrative': conclude([blind['status'], *result_statuses(comparison)])}
    report['usage'] = {'generated_images': 0, 'review_calls': vlm.calls,
                       'review_call_seconds': round(vlm.call_seconds, 3),
                       'review_elapsed_seconds': round(time.monotonic() - started, 3)}
    save_json_atomic(job_dir / 'review.json', report)
    write_review_md(job_dir, report)
    record = {'session_id': vlm.session_id, 'at': datetime.now(timezone.utc).isoformat(),
              'review_file': 'review.json', 'log_file': 'review_log.jsonl',
              'conclusion': report['conclusion'], 'layer_conclusions': report['layer_conclusions'],
              'reference_note': REFERENCE_NOTE, 'usage': report['usage']}
    # Preserve prior records and snapshot on repeat rather than silently replace evidence.
    archive = artifacts / f'review_{vlm.session_id}.json'
    save_json_atomic(archive, report)
    record['archive_file'] = str(archive.relative_to(job_dir))
    manifest.setdefault('review_records', []).append(record)
    usage = manifest.setdefault('usage', {})
    usage['review_calls'] = usage.get('review_calls', 0) + vlm.calls
    for key in ['review_call_seconds', 'review_elapsed_seconds']:
        usage[key] = round(usage.get(key, 0) + report['usage'][key], 3)
    save_json_atomic(job_dir / 'manifest.json', manifest)
    return report


def write_review_md(job_dir, report):
    lines = ['# 三层审核', '', f"结论：**{report['conclusion']}**", '', REFERENCE_NOTE, '',
             '规则：任何 fail、未知、缺项、超时或解析失败均未通过；无 fail 且有 minor 为有瑕疵但可选；全 pass 为通过。', '',
             '## 各层结论', '', *[f'- {key}: {value}' for key, value in report['layer_conclusions'].items()]]
    for layer in ['single', 'consistency']:
        lines.extend(['', f'## {layer}', ''])
        for result in report[layer]:
            lines.extend([f"### 第 {result['shot']} 镜", ''])
            if result['status'] != 'pass':
                lines.append('未通过：' + result.get('error', 'unknown'))
            else:
                for key, item in result['data'].items():
                    reason = item['reason'].replace('\n', ' ')
                    lines.append(f"- {key}: **{item['status']}** — {reason}")
    lines.extend(['', '## 面部裁切（并排图左参考，右当前）', ''])
    for face in report['faces']:
        lines.append(f"- 第 {face['shot']} 镜: {face['status']}; " +
                     (f"[裁切]({face['path']})" if face.get('path') else face.get('error', 'unknown')))
    for item in report['consistency']:
        if item.get('face_pair'):
            lines.append(f"- 第 {item['shot']} 镜 [面部对照]({item['face_pair']})")
    for key in ['narrative_blind', 'narrative_compare']:
        lines.extend(['', f'## {key}', '', '```json',
                      json.dumps(report[key], ensure_ascii=False, indent=2), '```'])
    lines.extend(['', '## 用量', '', '```json',
                  json.dumps(report['usage'], ensure_ascii=False, indent=2), '```', ''])
    (Path(job_dir) / 'review.md').write_text('\n'.join(lines), encoding='utf-8')
