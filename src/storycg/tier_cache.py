"""Evidence-backed tier reuse keyed by character and complete model file hashes."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from storycg.runner import _job_lock
from storycg.state import save_json_atomic

PROJECT = Path(__file__).resolve().parents[2]


class ModelHashUnavailable(ValueError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def model_fingerprint(identity, config):
    """Hash current bytes every time; filenames/mtime/training hashes never suffice.

    Read-only model paths may be mounts. No remote service/configuration is changed.
    An absent complete-file source disables caching instead of inventing a hash.
    """
    files = config.get('tier_cache', {}).get('model_files', {})
    def model_hash(folder, name):
        path = files.get(folder, {}).get(name)
        if not path:
            raise ModelHashUnavailable(f'complete model file path unavailable: {folder}/{name}')
        path = Path(path).expanduser()
        if not path.is_absolute(): path = PROJECT/path
        try: return file_sha256(path)
        except OSError as exc: raise ModelHashUnavailable(f'cannot hash {folder}/{name}: {exc}') from exc
    character = identity.get('character_tag')
    if not character: raise ModelHashUnavailable('canonical character tag unavailable')
    lora = identity.get('lora')
    return {'character': character, 'checkpoint_sha256': model_hash('checkpoints', config['checkpoint']),
            'lora_sha256': model_hash('loras', lora['name']) if lora else ''}


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
        if record['key_inputs'] != fingerprint or record['identity_tier'] not in ('L1', 'L2', 'L3', 'L4'):
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


def terminal_l4_evidence(root, identity, fingerprint):
    """Require the current session's full, valid, unsuccessful attempt budget."""
    from storycg.fallback import comparison_available, l3_comparison_passes
    from storycg.identity_enrichment import load_rules
    root = Path(root).resolve()
    terminal = identity.get('tier_evidence', {}).get('l3_terminal', {})
    limit = load_rules()['l3']['max_attempts']
    if (not fingerprint or terminal.get('model_fingerprint') != fingerprint
            or terminal.get('status') != 'exhausted' or terminal.get('max_attempts') != limit):
        return False
    try:
        session = root/terminal['session']
        if not session.resolve().is_relative_to(root/'l3'): return False
        if json.loads((session/'terminal.json').read_text()) != terminal: return False
        if terminal['attempts_file'] != str((session/'attempts.json').relative_to(root)): return False
        attempts = json.loads((root/terminal['attempts_file']).read_text())
        if [a['number'] for a in attempts] != list(range(1, limit+1)): return False
        expected = [str((session/f'comparison-{n}.json').relative_to(root)) for n in range(1, limit+1)]
        if terminal['comparison_paths'] != expected: return False
        for attempt, path in zip(attempts, expected):
            comparison = json.loads((root/path).read_text())
            if (comparison != attempt['comparison'] or not comparison_available(comparison)
                    or l3_comparison_passes(comparison)): return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def evidence_confirms(root, identity, fingerprint=None):
    """Pending, missing comparisons, and resource-only L4 outcomes are not evidence."""
    from storycg.fallback import comparison_available, current_l3_approval
    from storycg.tier_gate import evaluate_l2
    from storycg.tiers import identity_checklist
    evidence = identity.get('tier_evidence', {})
    if not evidence.get('validation_completed') or not comparison_available({'tier_evidence': evidence}):
        return False
    tier = identity.get('identity_tier')
    policy = json.loads((PROJECT/'config/review_reliability.json').read_text())
    rows = [row for pair in evidence['comparisons'] for row in pair.get('items', [])]
    gate = evaluate_l2(identity_checklist(identity), rows, policy)
    if tier in ('L1', 'L2'):
        return gate['identity_tier'] == 'L2' and bool(identity.get('lora')) == (tier == 'L1')
    if tier == 'L3': return current_l3_approval(identity)
    if tier == 'L4' and evidence.get('l4_reason') and gate['identity_tier'] != 'L2':
        return terminal_l4_evidence(root, identity, fingerprint)
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
        l3 = identity['tier_evidence'].get('l3', {})
        if l3.get('attempts_file'): candidates.append(root/l3['attempts_file'])
        terminal = identity['tier_evidence'].get('l3_terminal', {})
        if terminal:
            candidates.extend(root/p for p in terminal['comparison_paths'])
            candidates.extend([root/terminal['attempts_file'], root/terminal['session']/'terminal.json'])
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
    # The same bytes may have a new inventory filename. Keep evidence provenance
    # in the snapshot while generation uses the current available LoRA name.
    result['lora'] = copy.deepcopy(identity.get('lora'))
    result['tier_evidence'] = copy.deepcopy(source['tier_evidence'])
    artifact = result['tier_evidence'].pop('artifact_root', None)
    if artifact: result['tier_evidence']['source_artifact_root'] = str(Path(record['source_job'])/artifact)
    result['tier_model_fingerprint'] = copy.deepcopy(record['key_inputs'])
    result['tier_cache'] = {'hit': True, 'established_at_utc': record['established_at_utc'],
                            'source_job': record['source_job'], 'evidence_paths': record['evidence_paths']}
    result['status'] = 'needs_user' if result['identity_tier'] == 'L4' else 'locked'
    result['needs_user'] = result['identity_tier'] == 'L4'
    for field in ('reason', 'question', 'user_can_provide'):
        result.pop(field, None)
        if result['identity_tier'] == 'L4' and field in source: result[field] = copy.deepcopy(source[field])
    save_json_atomic(Path(root)/'identity.json', result)
    return result
