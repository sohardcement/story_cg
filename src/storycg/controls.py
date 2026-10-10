"""Frozen generation conditions and content-addressed reference uploads."""
import copy
import json
from pathlib import Path
from storycg.state import save_json_atomic
from storycg.reference import digest


def apply_options(config, options):
    config = copy.deepcopy(config)
    options = options or {}
    control = config.setdefault('identity_control', {})
    control['mechanism'] = options.get('identity_control', control.get('mechanism', 'ipadapter'))
    if control['mechanism'] not in ('none', 'ipadapter'):
        raise ValueError('identity_control must be none or ipadapter')
    # V2-28: old jobs may carry layout_mode in their options; only 'none' is still accepted.
    from storycg.config import check_removed_switches
    check_removed_switches({'layout_mode': options.get('layout_mode', 'none')})
    if not 0 <= control.get('ipadapter_strength', 0.4) <= 1:
        raise ValueError('invalid IPAdapter strength')
    if 'generation_seed' in options:
        config['generation_seed'] = options['generation_seed']
    if 'generation_seed' in config and (not isinstance(config['generation_seed'], int)
                                      or not 0 <= config['generation_seed'] < 2**64):
        raise ValueError('seed must be an unsigned 64-bit integer')
    return config


def upload_reference(root, path, client, *, expected_sha256=None):
    root = Path(root); path = Path(path)
    sha = digest(path)
    if expected_sha256 is not None and sha != expected_sha256:
        raise ValueError("reference image hash mismatch")
    record_path = root/'reference_uploads.json'
    records = json.loads(record_path.read_text()) if record_path.exists() else {}
    if sha not in records:
        result = client.upload_image(path)
        records[sha] = '/'.join(filter(None, [result.get('subfolder'), result['name']]))
        save_json_atomic(record_path, records)
    return records[sha]


def portrait_reference(root, portrait, key, client, mechanism, strength):
    detection = None
    if key == 'face' and mechanism == 'ipadapter':
        from storycg.detection_review import detect_face_record
        source = Path(root)/portrait['image']['path']
        if digest(source) != portrait['image']['sha256']:
            raise ValueError('portrait image hash mismatch')
        target = Path(root)/'detected_references'/(portrait['image']['sha256'] + '.png')
        detection,item = detect_face_record(source,target)
        if item is None:raise ValueError('face_not_detected: IPAdapter reference unavailable')
        item['path'] = str(target.relative_to(root))
    else:
        item = portrait[key]
    return {'mechanism': mechanism, 'strength': strength, 'path': item['path'],
            **({'detection': detection} if detection else {}),
            'sha256': item['sha256'], 'source_task': portrait['source_task'],
            'portrait_sha256': portrait['image']['sha256'],
            'uploaded_image': upload_reference(root, Path(root)/item['path'], client, expected_sha256=item['sha256'])}
