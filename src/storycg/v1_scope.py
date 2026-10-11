"""D23–D25 scope policy; consumes structured state, never guesses from prose.

V2-42: the group verdict helpers (person/narrative filters, scope context, render_delivery) are gone with
review_gate.py; what is left is the prop-item test and the delivery notice lines.
"""
from pathlib import Path
import json


def is_prop_item(key):
    return key == 'props' or key.startswith('prop_')


def delivery_scope_notes(root):
    """Fixed v2 delivery notice plus the saved storyboard adaptation, if any."""
    path = Path(root)/'storyboard.json'
    board = json.loads(path.read_text()) if path.exists() else {}
    note = board.get('story_note', '').strip()
    identity_path = Path(root)/'identity.json'
    identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
    cache = identity.get('tier_cache', {})
    receipt_path = Path(root)/'tier/cache-reuse.json'
    if not cache.get('hit') and receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('key_inputs') == identity.get('tier_model_fingerprint'):
            cache = receipt
    tier_note = ['层级沿用缓存（'+cache['established_at_utc'][:10]+'）。'] if cache.get('hit') else []
    return ['道具未经审核。'] + ([note] if note else []) + tier_note
