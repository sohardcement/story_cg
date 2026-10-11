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
    return ['道具未经审核。'] + ([note] if note else [])
