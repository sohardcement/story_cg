"""V2-32: display-only helpers for the confirmation summary, status and result.md.

Nothing here changes a review conclusion, a screen, a pick or a stored verdict. It only reads the
records a job already wrote (portrait.json, portrait_attempts.json, plan_log.json) and shapes text.
"""
import json
from pathlib import Path

REASON_CHARS = 160


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding='utf-8'))


def short(text, limit=REASON_CHARS):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def portrait_review(root):
    """The raw portrait audit as the user should see it. Only records; never a verdict of its own."""
    root = Path(root)
    portrait = read_json(root / 'portrait.json')
    if not portrait:
        return None
    raw_status = portrait.get('raw_status', portrait.get('status'))
    review = {'raw_status': raw_status, 'raw_conclusion': portrait.get('raw_conclusion'), 'failed_items': [],
              'note': None}
    if raw_status == 'pass':
        return review
    attempts = read_json(root / 'portrait_attempts.json', [])
    approved = portrait.get('approved_attempt')
    attempt = next((a for a in attempts if a.get('attempt') == approved), attempts[-1] if attempts else {})
    # V2-33: the raw conclusion also counts the detection review (accessory colour, protagonist count); its
    # failing items were missing here. not_applicable never decides a group (result_statuses), so it is not listed.
    sources = [('audit', ((attempt.get('audit') or {}).get('data')) or {}),
               ('detection', (((attempt.get('detection_items') or {}).get('review') or {}).get('data')) or {})]
    for source, data in sources:
        for key, item in data.items():
            if isinstance(item, dict) and item.get('status') not in ('pass', 'minor', 'not_applicable'):
                review['failed_items'].append({'item': key, 'status': item.get('status'),
                                               'reason': short(item.get('reason'), 240), 'source': source,
                                               'reliable': item.get('reliability') == 'reliable'})
    review['note'] = '定妆图审核只记录、不拦截，请用户看图确认'
    return review


def compile_rewrites(root):
    """Texts of the automatic rewrites made by the last valid compile (plan_log.json); [] when none."""
    calls = read_json(Path(root) / 'plan_log.json', {'calls': []}).get('calls', [])
    valid = [c for c in calls if c.get('status') == 'valid']
    return [r.get('text', '') for r in valid[-1].get('rewrites', [])] if valid else []
