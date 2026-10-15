"""V2-16 (D59): per-shot check for extra body parts, with the V2-15 method M3 (one vision question).

The prompt and schema live in config/cycle.json under "extra_body_part" (source: V2-15). The subject
wording is built from the identity: 1girl / 1boy map to a single woman / man, otherwise a neutral
"one main character" with no gender word. Reliability comes from config/review_reliability.json.
"""
import json
from pathlib import Path

from storycg import review

CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config/cycle.json'
ITEM = 'extra_body_part'


def settings():
    return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))[ITEM]


def subject_clause(identity, cfg=None):
    cfg = cfg or settings()
    tokens = {token.strip() for token in str(identity.get('identity_block', '')).split(',')}
    for token, clause in cfg['subject_clauses'].items():
        if token in tokens:
            return clause
    return cfg['neutral_subject_clause']


def prompt_for(identity, cfg=None):
    cfg = cfg or settings()
    return cfg['prompt'].replace('{subject_clause}', subject_clause(identity, cfg))


def should_check(detection_items):
    """Skip the question when the protagonist count gate already reports a duplicate (V2-16 item 2)."""
    return (detection_items or {}).get('protagonist_count', {}).get('status') != 'duplicate'


def check(vlm, path, identity, purpose):
    """One vision question about this image. Returns the data item (fail when extra_part is true)."""
    cfg = settings()
    result = vlm.call(purpose, review.messages(prompt_for(identity, cfg), [path]), cfg['schema'], [path])
    if result.get('status') != 'pass' or not result.get('data'):
        return {'status': 'fail', 'error': result.get('error', 'extra body part audit failed'), 'data': {}}
    data = result['data']
    item = {'status': 'fail' if data['extra_part'] else 'pass', 'reason': data['description'],
            'location': data['location'], 'calibration': cfg['source']}
    wrapper = review.annotate_reliability({'status': 'pass', 'data': {ITEM: item}}, review.load_reliability({}))
    return {'status': 'pass', 'data': wrapper['data']}


def merge_into(consistency, extra):
    """Add the check to a consistency result, the same way the supplementary review is merged."""
    consistency['extra_body_part'] = extra
    if extra['status'] != 'pass':
        consistency['status'] = 'fail'
        consistency['error'] = extra.get('error', 'extra body part audit failed')
    else:
        consistency.setdefault('data', {}).update(extra['data'])
    return consistency


def item_of(audit):
    """The check's data item: the screen's top-level extra_body_part (V2-61), or the consistency item of an older audit."""
    audit = audit or {}
    extra = audit.get(ITEM)
    if isinstance(extra, dict) and 'data' in extra:
        return (extra.get('data') or {}).get(ITEM, {})
    return (audit.get('consistency') or {}).get('data', {}).get(ITEM, {})


def is_positive(audit):
    """True when the screen (or an older audit's consistency result) reports an extra-body-part failure."""
    return item_of(audit).get('status') == 'fail'


def adopt(before_positive, after_positive):
    """Candidate adoption (V2-16 item 4): a negative candidate needs the improvement check (done by the
    caller); a still-positive candidate is adopted only over a positive source and only if it improves."""
    return (not after_positive) or before_positive


def strip(results):
    """Remove the check from repair findings once a seed re-roll for it has been tried."""
    stripped = []
    for result in results:
        data = {k: v for k, v in result.get('data', {}).items() if k != ITEM}
        stripped.append({**result, 'data': data})
    return stripped


def known_issue(number, audit):
    item = item_of(audit)
    return (f'- 第 {number} 镜疑似有多余身体局部（{item.get("location", "unknown")}：{item.get("reason", "")}），待人工复看')
