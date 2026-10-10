"""D58 (V2-14): a shot that asks for closed eyes has no eye colour to judge.

Shared by the repair consistency review (cycle.py) and the delivery-time review (review.py).
Imports only prompt_rules and tags, so neither review module creates an import cycle.
"""
from storycg.prompt_rules import companion_tags_for_shot
from storycg.tags import canonical_tag

EYE_COLOUR_KEY = 'eye_color'
NOT_APPLICABLE_REASON = '分镜要求闭眼，瞳色不适用'


def requires_closed_eyes(shot, config=None):
    """The shot asks for closed eyes via pose, expression, tags or a companion rule."""
    values = [shot.get('pose'), shot.get('expression'), *shot.get('tags', []),
              *companion_tags_for_shot(shot, config)]
    return 'closed_eyes' in {canonical_tag(value) for value in values if isinstance(value, str)}


def drop_eye_colour_check(checks, shot, config=None):
    """Remove eye_color from a checklist when the shot requires closed eyes; return whether it did."""
    if requires_closed_eyes(shot, config):
        checks.pop(EYE_COLOUR_KEY, None)
        return True
    return False


def mark_eye_colour_not_applicable(result):
    """Record eye_color as not_applicable in a consistency result (never counted as a failure)."""
    result.setdefault('data', {})[EYE_COLOUR_KEY] = {
        'status': 'not_applicable', 'reliability': 'not_applicable', 'reason': NOT_APPLICABLE_REASON}
    return result
