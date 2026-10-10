"""Evidence-backed tier reuse keyed by character and ComfyUI-reported model identity."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from storycg.runner import _job_lock
from storycg.state import save_json_atomic

PROJECT = Path(__file__).resolve().parents[2]


class ModelIdentityUnavailable(ValueError):
    pass


def file_sha256(path):
    """Local evidence files only; model files live on the ComfyUI host."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def model_identity(client, folder, name):
    """Name plus size as reported by this run's ComfyUI inventory response.

    A failed request, an entry the inventory does not list, or a missing size leaves the
    identity unknown. Nothing else (local bytes, mtime, training hashes) may substitute it.
    """
    try:
        files = client.list_model_files(folder)
    except (AttributeError, httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        raise ModelIdentityUnavailable(f'ComfyUI {folder} inventory unavailable: {exc}') from exc
    match = next((item for item in files or [] if item.get('name') == name), None)
    if match is None:
        raise ModelIdentityUnavailable(f'{name} is not listed in the ComfyUI {folder} inventory')
    size = match.get('size')
    if size is None:
        raise ModelIdentityUnavailable(f'ComfyUI {folder} reported no size for {name}')
    return {'name': name, 'size': size}


def model_fingerprint(identity, config, client):
    """Key inputs: character tag, checkpoint {name, size}, character LoRA {name, size} and the
    style LoRA set (V2-20): {name, size, strength_model, strength_clip} per style LoRA, in
    config order. An empty style list is part of the key too.

    Names and sizes come from the inventory response of this run. `modified` is
    deliberately absent: copying a file changes it without changing the model.
    """
    character = identity.get('character_tag')
    if not character: raise ModelIdentityUnavailable('canonical character tag unavailable')
    lora = identity.get('lora')
    style = [{**model_identity(client, 'loras', item['name']),
              'strength_model': item['strength_model'], 'strength_clip': item['strength_clip']}
             for item in config.get('style_loras', [])]
    return {'character': character,
            'checkpoint': model_identity(client, 'checkpoints', config['checkpoint']),
            'lora': model_identity(client, 'loras', lora['name']) if lora else '',
            'style_loras': style}


def cache_key(fingerprint):
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()


def cache_path(config):
    value = config.get('tier_cache', {}).get('path')
    if not value: return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT/path).resolve()


def cached_tier(config, fingerprint):
    path = cache_path(config)
    if path is None: return None
    try:
        cache = json.loads(path.read_text())
        if cache.get('version') != 1: return None
        record = cache['entries'][cache_key(fingerprint)]
        if record['key_inputs'] != fingerprint or record['identity_tier'] not in ('L1', 'L2'):
            return None
        if not record['evidence_paths']: return None
        for item in record['evidence_paths']:
            if file_sha256(item['path']) != item['sha256']: return None
        snapshot = json.loads(Path(record['evidence_paths'][0]['path']).read_text())
        if (snapshot != record['identity'] or snapshot['identity_tier'] != record['identity_tier']
                or snapshot['character_tag'] != fingerprint['character']): return None
        return copy.deepcopy(record)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def evidence_confirms(root, identity, fingerprint=None):
    """Pending, missing or unavailable comparisons are not evidence. Only L1 (LoRA validated) and L2 (WAI list) are cached."""
    from storycg.tiers import comparison_available, identity_checklist
    from storycg.tier_gate import evaluate_l2
    evidence = identity.get('tier_evidence', {})
    if not evidence.get('validation_completed') or not comparison_available({'tier_evidence': evidence}):
        return False
    tier = identity.get('identity_tier')
    policy = json.loads((PROJECT/'config/review_reliability.json').read_text())
    rows = [row for pair in evidence['comparisons'] for row in pair.get('items', [])]
    gate = evaluate_l2(identity_checklist(identity), rows, policy)
    if tier in ('L1', 'L2'):
        return gate['identity_tier'] == 'L2' and bool(identity.get('lora')) == (tier == 'L1')
    return False


def store_tier(root, identity, config, fingerprint):
    path = cache_path(config)
    if path is None: return None
    root = Path(root).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _job_lock(path.parent):
        previous = cached_tier(config, fingerprint)
        if previous: return previous  # First established evidence wins for this key.
        if not evidence_confirms(root, identity, fingerprint): return None
        key = cache_key(fingerprint)
        snapshot = root/'tier/cache-evidence'/(key+'.json')
        # Dedicated evidence snapshots are immutable across identity.json updates.
        save_json_atomic(snapshot, identity)
        evidence = [{'path': str(snapshot), 'sha256': file_sha256(snapshot)}]
        artifact = identity['tier_evidence'].get('artifact_root')
        candidates = [root/'tier_gate.json']
        if artifact: candidates.append(root/artifact/'l2/comparison-v3.json')
        for candidate in candidates:
            if candidate.is_file(): evidence.append({'path': str(candidate.resolve()), 'sha256': file_sha256(candidate)})
        record = {'key_inputs': fingerprint, 'identity_tier': identity['identity_tier'],
                  'established_at_utc': datetime.now(timezone.utc).isoformat(),
                  'source_job': str(root), 'evidence_paths': evidence,
                  'identity': copy.deepcopy(identity)}
        try: cache = json.loads(path.read_text())
        except (OSError, ValueError): cache = {'version': 1, 'entries': {}}
        if cache.get('version') != 1 or not isinstance(cache.get('entries'), dict):
            cache = {'version': 1, 'entries': {}}
        cache['entries'][key] = record
        save_json_atomic(path, cache)
        return record


def reuse_tier(root, identity, record):
    root = Path(root).resolve()
    receipt = {'hit': True, 'established_at_utc': record['established_at_utc'],
        'key_inputs': record['key_inputs'], 'source_job': record['source_job'],
        'evidence_paths': record['evidence_paths']}
    save_json_atomic(root/'tier/cache-reuse.json', receipt)
    # A source job already contains this evidence. Keep its portrait identity key
    # and relative artifact paths stable; only the external reuse receipt changes.
    if str(root) == record['source_job']:
        result = copy.deepcopy(identity)
        source = record['identity']
        for field in ('identity_tier', 'lora', 'tier_evidence', 'tier_model_fingerprint',
                      'status', 'needs_user', 'reason', 'question', 'user_can_provide', 'tier_cache'):
            if field in source: result[field] = copy.deepcopy(source[field])
            else: result.pop(field, None)
        result['lora'] = copy.deepcopy(identity.get('lora'))
        save_json_atomic(root/'identity.json', result)
        return result
    result = copy.deepcopy(identity)
    source = record['identity']
    result['identity_tier'] = record['identity_tier']
    # The key carries the inventory name, so a hit means the current selection matches.
    result['lora'] = copy.deepcopy(identity.get('lora'))
    result['tier_evidence'] = copy.deepcopy(source['tier_evidence'])
    artifact = result['tier_evidence'].pop('artifact_root', None)
    if artifact: result['tier_evidence']['source_artifact_root'] = str(Path(record['source_job'])/artifact)
    result['tier_model_fingerprint'] = copy.deepcopy(record['key_inputs'])
    result['tier_cache'] = {'hit': True, 'established_at_utc': record['established_at_utc'],
                            'source_job': record['source_job'], 'evidence_paths': record['evidence_paths']}
    result['status'] = 'locked'
    result['needs_user'] = False
    for field in ('reason', 'question', 'user_can_provide'):
        result.pop(field, None)
    save_json_atomic(Path(root)/'identity.json', result)
    return result
