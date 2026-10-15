"""Fail-closed three-layer review of existing images; never generates images."""

import base64
import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from PIL import Image, ImageOps

from storycg.closed_eyes import drop_eye_colour_check, mark_eye_colour_not_applicable
from storycg.net_errors import json_body
from storycg.state import save_json_atomic

TEXT = {'type': 'string', 'minLength': 1}
GRADE = {'type': 'object', 'additionalProperties': False,
         'required': ['status', 'reason'], 'properties': {
             'status': {'enum': ['pass', 'minor', 'fail', 'not_visible']}, 'reason': TEXT}}
SINGLE_KEYS = ['body', 'hands', 'action', 'composition', 'style']
REFERENCE_NOTE = '参考=第1镜，非定妆图'


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


def grading_schema(keys, *, visibility=False, observability=False):
    grade = copy.deepcopy(GRADE)
    if visibility:
        grade['required'].append('visible')
        grade['properties']['visible'] = {'type': 'boolean'}
    if observability:
        grade['required'] += ['should_be_visible', 'visibility_reason']
        grade['properties'].update({
            'should_be_visible': {'type': 'boolean'},
            'visibility_reason': {'enum': ['visible', 'absent', 'angle', 'occluded', 'out_of_frame', 'too_small']}})
    return object_schema({key: grade for key in keys})


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
                 max_seconds=3600, timeout=180, request_max_seconds=180):
        # max_seconds=None: no limit on the total time; each request still has its own cap (request_max_seconds).
        self.job_dir = Path(job_dir)
        self.url = url.rstrip('/')
        self.owned = client is None
        self.http = client or httpx.Client(base_url=self.url, timeout=timeout, trust_env=False)
        self.model = model
        self.session_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.max_seconds = max_seconds
        self.request_max_seconds = request_max_seconds
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
            available = [m for m in json_body(response)['data'] if m.get('loaded', True)
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

    def post_with_limit(self, request, timeout, limit):
        """Total-time cap. httpx timeouts only bound each wait, so a server trickling bytes never trips them.
        The post runs in a daemon thread that reads the whole body; if it is not done after `limit` seconds,
        the caller gives up. The abandoned thread only fills its own `outcome` dict, never self or the log."""
        outcome = {}

        def send():
            try:
                outcome['response'] = self.http.post(self.url + '/chat/completions', json=request, timeout=timeout)
            except Exception as exc:
                outcome['error'] = exc
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        worker.join(limit)
        if worker.is_alive():
            raise TimeoutError(f'vision request exceeded {limit:.1f} s')
        if 'error' in outcome:
            raise outcome['error']
        return outcome['response']

    def call(self, purpose, message_list, schema, paths=()):
        over_time = self.max_seconds is not None and time.monotonic() - self.started >= self.max_seconds
        if self.calls >= self.max_calls or over_time:
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
            timeout = 180
            limit = self.request_max_seconds
            if self.max_seconds is not None:
                remaining = self.max_seconds - (time.monotonic() - self.started)
                if remaining <= 0:
                    raise TimeoutError('review time budget exhausted')
                timeout = min(180, remaining)
                limit = min(limit, remaining)
            response = self.post_with_limit(request, timeout, limit)
            raw = response.text
            response.raise_for_status()
            envelope = json_body(response)
            choice = envelope['choices'][0]
            if choice.get('finish_reason') not in (None, 'stop'):
                raise ValueError('incomplete or refused response')
            data = json.loads(choice['message']['content'])
            if schema == BBOX_SCHEMA and data.get('found'):
                bbox, converted = normalize_bbox(data['bbox'], list(paths)[0])
                if converted:
                    result['bbox_conversion'] = {'original': data['bbox'], 'normalized': bbox}
                    self.log({'event': 'bbox_conversion', 'call_id': call_id,
                              **result['bbox_conversion']})
                data['bbox'] = bbox
            if schema == BBOX_SCHEMA and not data.get('found') and data.get('bbox') != [0, 0, 0, 0]:
                raise ValueError('absent face requires zero bbox')
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
    from storycg.identity_enrichment import outfit_hair_ornaments
    identity = outfit_hair_ornaments(identity)
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
        elif any(word in tag for word in ('earring', 'necklace', 'bracelet', 'jewelry', 'accessory')):
            category = 'accessories'
        elif any(word in tag for word in ('hair', 'bangs', 'tail', 'braid')):
            category = 'hairstyle'
        else:
            category = 'signature_features'
        groups[category]['expected'].append(item)
    for item in identity.get('default_outfit_tags', []):
        if item.get('state_scope') == 'outfit':
            continue
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



OBSERVABILITY_PROMPT = (
    'For EVERY feature first judge whether its expected region SHOULD BE VISIBLE in the CURRENT '
    'image given the actual camera angle, framing, occlusion and pixel size, independently of whether '
    'the feature is present. Report should_be_visible and visibility_reason. '
    'If the region should be inspectable but the expected feature is missing, should_be_visible=true, '
    'visible=false, visibility_reason=absent, status=fail. Absence requires concrete evidence of the '
    'exposed region, never just failure to see the feature. '
    'If angle, occlusion, cropping or too few pixels prevent inspection, should_be_visible=false, '
    'visible=false, status=not_visible; visibility_reason must be angle, occluded, out_of_frame or '
    'too_small. Explain the region and cause in reason. Never infer absence from non-observation. '
    'If inspectable and present, should_be_visible=true, visible=true, visibility_reason=visible; '
    'grade the actual match as pass/minor/fail. For grouped traits assess EACH expected part in reason; '
    'an unobservable required part is not_visible unless another inspectable part has a mismatch. '
    'Empty expectations are unsourced: do not invent canonical features. ')
VISIBILITY_PROMPT = (
    'First determine visible=true/false for EVERY item from actual pixels, before grading. '
    'If any required part cannot be observed (cropped, occluded, back view or too few pixels '
    'to distinguish its attribute), visible=false and status=not_visible; never pass it from '
    'the expectation or reference alone. Empty expectations are not required: compare only '
    'visible reference details without inventing canonical traits. '
    'pass=clear correct match; minor=small discrepancy; fail=visible mismatch; '
    'not_visible=not observable. Give concrete evidence in reason. ')
SINGLE_PROMPT = (
    'Audit body anatomy, hands, action against the shot, composition and style. '
    'Do not infer intended action. '
    'For framing_observation describe actual visible body extent (head to where) in reason '
    'and classify lowest_visible; do not judge requested framing. ')
CONSISTENCY_PROMPT = (
    'Compare script-owned identity and outfit checklist only. Images: full reference, current '
    'full image, optionally face pair LEFT reference RIGHT current. For each item separately '
    'compare reference/current AND sourced expectation. Explicit outfit changes are allowed '
    'only when declared. Also audit supplied scene fields against current/previous state. ')
FRAMING_OBSERVATION = object_schema({
    'lowest_visible': {'enum': ['face', 'shoulders', 'chest', 'waist', 'hips', 'thighs',
                                'knees', 'calves', 'feet', 'unknown']}, 'reason': TEXT})


def normalize_bbox(bbox, path):
    if (len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                             or not math.isfinite(v) or v < 0 for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError('invalid face bbox')
    converted = any(v > 1 for v in bbox)
    if converted:
        with Image.open(path) as image:
            w, h = image.size
        if any(v > limit for v, limit in zip(bbox, [w, h, w, h])):
            raise ValueError('pixel bbox exceeds image dimensions')
        bbox = [v / limit for v, limit in zip(bbox, [w, h, w, h])]
    return bbox, converted


def enforce_visibility(result):
    if result['status'] == 'pass':
        for item in result['data'].values():
            if item.get('should_be_visible') is False:
                item['status'] = 'not_visible'
            elif item.get('should_be_visible') is True and item.get('visible') is False:
                item['status'] = 'fail'
                item['visibility_reason'] = 'absent'
            elif item.get('visible') is False:
                item['status'] = 'not_visible'
    return result


def visibility_summary(results):
    keys = set(k for r in results if r['status'] == 'pass' for k in r['data'])
    required = {k for r in results for k, v in r.get('checklist', {}).items() if v.get('expected')}
    if not any('checklist' in r for r in results):
        required = keys
    return {'unreviewed': [{'shot': r.get('shot'), 'item': k, 'note': '未经审核'}
                          for r in results for k, v in r.get('data', {}).items()
                          if v.get('status') == 'not_visible' and k in required],
            'all_shots_not_visible': sorted(k for k in required if results and all(
                r['status'] == 'pass' and r['data'].get(k, {}).get('status') == 'not_visible'
                for r in results))}


def framing_grade(expected, observed, config, *, pose=None):
    from storycg.framing import allowed_parts
    allowed = allowed_parts(config, expected, pose)
    return {'status': 'pass' if allowed and observed['lowest_visible'] in allowed else 'fail',
            'reason': f"expected={expected}; allowed={allowed}; observed={observed['lowest_visible']}; "
                      + observed['reason']}


def reliability_key(key):
    if key in ('extra_protagonist', 'outfit_detail_vs_portrait', 'accessories_zoom', 'hair_ornaments_zoom'):
        return key
    if key.startswith('outfit_'):
        return 'outfit'
    if key.startswith('prop_'):
        return 'props'
    if key in ('hair_ornaments', 'accessories'):
        return 'accessories'
    return key


def load_reliability(config):
    path = Path(config.get('review_reliability_file', Path(__file__).resolve().parents[2] /
                           'config/review_reliability.json'))
    return json.loads(path.read_text()).get('items', {}) if path.exists() else {}


def annotate_reliability(result, items):
    for key, item in result.get('data', {}).items():
        calibrated = items.get(key, items.get(reliability_key(key), {}))
        status = calibrated.get('status', 'unverified')
        item['reliability'] = status
        item['verified'] = status == 'reliable' and item.get('status') == 'pass'
        note = calibrated.get('note', '')
        notes = [] if status == 'reliable' else ['该项未经可靠审核']
        if isinstance(note, str) and note.strip():
            notes.append(note.strip())
        item['reliability_note'] = '；'.join(notes)
    return result


def reliability_note_lines(report=None, config=None):
    """Render configured notes without inventing grades or calibration rates."""
    items = dict(load_reliability(config or {}))
    for key, item in (report or {}).get('reliability', {}).items():
        if isinstance(item.get('note'), str) and item['note'].strip():
            items[key] = item
    return [f'- {key}: {item["note"].strip()}' for key, item in items.items()
            if isinstance(item.get('note'), str) and item['note'].strip()]


def audit_single(vlm, path, entry, props, config, purpose):
    schema = grading_schema(SINGLE_KEYS, visibility=True)
    if entry['shot'].get('framing'):
        schema['properties']['framing_observation'] = FRAMING_OBSERVATION
        schema['required'].append('framing_observation')
    result = vlm.call(purpose, messages(VISIBILITY_PROMPT + SINGLE_PROMPT +
        'Expected shot: ' + json.dumps({k:v for k,v in entry['shot'].items()
            if k not in ('key_props','prop_tags')}, ensure_ascii=False), [path]), schema, [path])
    enforce_visibility(result)
    if result['status'] == 'pass' and 'framing_observation' in result['data']:
        observed = result['data'].pop('framing_observation')
        result['framing_observation'] = observed
        shot = entry['shot']
        pose = shot.get('pose') if 'pose' in shot else shot.get('tags', [])
        if shot.get('pose_tag_omitted'): pose = None
        result['data']['framing'] = framing_grade(shot['framing'], observed, config, pose=pose)
    return annotate_reliability(result, load_reliability(config))


def audit_consistency(vlm, paths, checks, config, purpose, reference_note='参考=已审核定妆图'):
    result = vlm.call(purpose, messages(OBSERVABILITY_PROMPT + CONSISTENCY_PROMPT + reference_note + '. ' +
        'Checklist: ' + json.dumps(checks, ensure_ascii=False), paths),
        grading_schema(checks, visibility=True, observability=True), paths)
    enforce_visibility(result)
    result['checklist'] = checks
    return annotate_reliability(result, load_reliability(config))

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
    # V2-14: not_applicable items are neither pass nor fail; they never decide the group.
    return [item['status'] for item in result['data'].values() if item.get('status') != 'not_applicable']


BBOX_SCHEMA = object_schema({
    'found': {'type': 'boolean'},
    'bbox': {'type': 'array', 'minItems': 4, 'maxItems': 4,
             'items': {'type': 'number'}}, 'reason': TEXT})
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


def review_job(job_dir, config, *, client=None, max_calls=30, max_seconds=3600, per_shot_consistency=None):
    """per_shot_consistency: P0.2 (D97) switch for the identity/outfit/scene review of each shot. None reads
    config final_review.per_shot_consistency (True when absent: a direct review_job call keeps its old behaviour).
    cycle.full_review passes the delivery default, which is off."""
    if per_shot_consistency is None:
        per_shot_consistency = (config.get('final_review') or {}).get('per_shot_consistency', True)
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
    portrait = None
    reference_note = REFERENCE_NOTE
    if (job_dir/'portrait.json').exists():
        from storycg.reference import require_portrait
        portrait = require_portrait(job_dir)
        reference_note = '参考=已审核定妆图'
        reference_full = artifacts/'reference_portrait.png'
        reference_face = artifacts/'reference_face.png'
        with Image.open(job_dir/portrait['image']['path']) as source:
            source.convert('RGB').save(reference_full)
        from storycg.detection_review import detect_face_record
        reference_detection,reference_crop=detect_face_record(reference_full,reference_face)
    report = {'version': 2, 'session_id': vlm.session_id, 'reference_note': reference_note, 'reference': portrait,
              'inputs': inputs, 'faces': [], 'consistency': [],
              'rule': 'any fail/unknown/missing/error => 未通过; otherwise any minor => 有瑕疵但可选; all pass => 通过'}
    if portrait:
        report['reference_face_detection'] = reference_detection
    statuses = []
    try:
        from storycg.detection_review import detect_face_record, blind_thumbnails
        for number, path in enumerate(paths, 1):
            result,crop = detect_face_record(path,artifacts/f'face_{number:02d}.png')
            face = {'shot':number,'detection':result,'status':'pass',
                    'face_status':'detected' if crop else 'face_not_detected'}
            if crop:
                face.update(crop)
                face['path'] = str(Path(crop['path']).relative_to(job_dir))
            else:
                face['error'] = 'face_not_detected'
            report['faces'].append(face)
            # A face-free back view is an observation, not an extra visual failure.
            statuses.append(face['status'])
        report['per_shot_consistency'] = 'run' if per_shot_consistency else 'skipped'
        for number, (path, entry, face) in enumerate(zip(paths, board['shots'], report['faces']) if per_shot_consistency else (), 1):
            checklist = identity_checklist(identity, entry['outfit'])
            previous_scene = board['shots'][number - 2]['scene'] if number > 1 else None
            checks = {**checklist, **scene_checklist(entry['scene'], previous_scene),
                      'scene_transition': {'expected': {'previous': previous_scene,
                                                       'current': entry['scene']}, 'source': 'storyboard.json:scene'}}
            compare_paths = [reference_full if portrait else paths[0], path]
            pair = None
            if face.get('path') and (bool(reference_crop) if portrait else bool(report['faces'][0].get('path'))):
                pair = artifacts / f'face_pair_{number:02d}.png'
                face_pair(reference_face if portrait else job_dir / report['faces'][0]['path'],
                          job_dir / face['path'], pair)
                compare_paths.append(pair)
            # V2-14 rework 2 (D58): a closed-eye shot requests no eye colour in the delivery-time review either.
            eyes_not_applicable = drop_eye_colour_check(checks, entry['shot'], config)
            result = audit_consistency(vlm, compare_paths, checks, config, f'consistency_{number:02d}', reference_note)
            if eyes_not_applicable:
                mark_eye_colour_not_applicable(result)
            if pair:
                result['face_pair'] = str(pair.relative_to(job_dir))
            report['consistency'].append({'shot': number, 'checklist': checks, **result})
            statuses.extend(result_statuses(result))
        blind_paths,thumbnail_records = blind_thumbnails(paths,artifacts/'blind',
            long_edge=config.get('blind_long_edge'))
        report['blind_thumbnails'] = thumbnail_records
        vlm.log({'event':'blind_thumbnail','records':thumbnail_records})
        blind = vlm.call('narrative_blind', blind_messages(blind_paths), BLIND_SCHEMA, blind_paths)
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
    report['visibility'] = visibility_summary(report['consistency'])
    report['reliability'] = load_reliability(config)
    report['conclusion'] = conclude(statuses)
    report['layer_conclusions'] = {
        'consistency': conclude([*(f['status'] for f in report['faces']),
                                *(s for r in report['consistency'] for s in result_statuses(r))]),
        'narrative': conclude([blind['status'], *result_statuses(comparison)])}
    report['usage'] = {'generated_images': 0, 'review_calls': vlm.calls,
                       'review_call_seconds': round(vlm.call_seconds, 3),
                       'review_elapsed_seconds': round(time.monotonic() - started, 3)}
    save_json_atomic(job_dir / 'review.json', report)
    write_review_md(job_dir, report, config)
    record = {'session_id': vlm.session_id, 'at': datetime.now(timezone.utc).isoformat(),
              'review_file': 'review.json', 'log_file': 'review_log.jsonl',
              'conclusion': report['conclusion'], 'layer_conclusions': report['layer_conclusions'],
              'reference_note': reference_note, 'reference': portrait, 'usage': report['usage'],
              'visibility': report['visibility'], 'reliability': report['reliability'],
              'blind_thumbnails': report['blind_thumbnails'],
              'item_reviews': {'consistency': report['consistency']}}
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


def write_review_md(job_dir, report, config=None):
    lines = ['# 三层审核', '', f"结论：**{report['conclusion']}**", '', report['reference_note'], '',
             '规则：任何 fail、未知、缺项、超时或解析失败均未通过；无 fail 且有 minor 为有瑕疵但可选；全 pass 为通过。', '',
             '## 各层结论', '', *[f'- {key}: {value}' for key, value in report['layer_conclusions'].items()]]
    notes = reliability_note_lines(report, config)
    if notes:
        lines.extend(['', '## 审核项说明', '', *notes])
    lines.extend(['', '## consistency', ''])
    for result in report['consistency']:
        lines.extend([f"### 第 {result['shot']} 镜", ''])
        if result['status'] != 'pass':
            lines.append('未通过：' + result.get('error', 'unknown'))
        else:
            for key, item in result['data'].items():
                reason = item['reason'].replace('\n', ' ')
                lines.append(f"- {key}: **{item['status']}** — {reason}; {item.get('reliability_note', '该项未经可靠审核')}")
    lines.extend(['', '## 未经审核的不可见必需项', '', '```json',
                  json.dumps(report.get('visibility', {}), ensure_ascii=False, indent=2), '```'])
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
