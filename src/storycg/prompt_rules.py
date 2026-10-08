"""Prompt-side rules that shape the assembled conditioning (TASK-v2 §2.2-8).

Three config-driven rules, none of which mention any character or test story:
scene object caps by framing, action-invoking props kept out of scene blocks,
and companion tags for actions that imply an occlusion state (e.g. closed eyes).
The fixed negative tail also lives here so every caller shares one de-duped list.
"""
from storycg.tags import canonical_tag


def scene_rules(config):
    return (config or {}).get('scene_rules') or {}


def _object_limit(framing, rules):
    limits = rules.get('object_limits') or {}
    return limits.get(framing, 3)


def _non_object_set(rules):
    return {canonical_tag(tag) for tag in rules.get('non_object_tags', [])}


def _action_invoking_set(rules):
    return {canonical_tag(tag) for tag in (rules.get('action_invoking_props') or {})}


def filter_scene_tags(framing, scene_tags, prop_tags, config=None):
    """Return (kept, dropped) scene tags under the framing's object budget.

    Non-object tags (setting/place/time/weather/light) never count. Objects are
    dropped beyond the budget; action-invoking objects are dropped outright
    unless they are the shot's one declared prop tag.
    """
    rules = scene_rules(config)
    limit = _object_limit(framing, rules)
    non_objects = _non_object_set(rules)
    action_invoking = _action_invoking_set(rules)
    declared = {canonical_tag(tag) for tag in (prop_tags or [])}
    entries = [(tag, canonical_tag(tag)) for tag in scene_tags]
    # The declared prop wins budget priority over ambient objects.
    keep, budget = set(), 0
    for tag, value in entries:
        if value in non_objects:
            keep.add(tag)
        elif value in declared and budget < limit:
            keep.add(tag); budget += 1
    for tag, value in entries:
        if tag in keep or value in non_objects:
            continue
        if value in action_invoking and value not in declared:
            continue
        if budget < limit:
            keep.add(tag); budget += 1
    kept = [tag for tag, _ in entries if tag in keep]
    dropped = [tag for tag, _ in entries if tag not in keep]
    return kept, dropped


def companion_rules(config):
    if config is None:
        from storycg.config import load_config
        config = load_config('config/default.json')
    return config.get('action_companion_tags') or {}


def companion_tags_for_shot(shot, config=None):
    """Companion tags the shot's action implies, e.g. sleeping -> closed eyes."""
    table = companion_rules(config)
    keys = [shot.get('pose')] if shot.get('pose') else list(shot.get('tags', []))
    required = []
    for key in keys:
        rule = table.get(canonical_tag(key))
        if rule:
            required.extend(rule.get('require', []))
    return list(dict.fromkeys(required))


def companion_conflicts(board, config=None):
    """Shots whose written tags contradict a companion requirement."""
    table = companion_rules(config)
    problems = []
    for entry in board.get('shots', []):
        shot = entry.get('shot', {})
        pose = shot.get('pose')
        if not isinstance(pose, str):
            continue  # Malformed poses are rejected by the contract validator.
        rule = table.get(canonical_tag(pose))
        if not rule:
            continue
        written = {canonical_tag(tag) for tag in shot.get('tags', [])}
        written.add(canonical_tag(shot.get('expression') or ''))
        conflicts = sorted({canonical_tag(c) for c in rule.get('conflicts', [])} & written)
        if conflicts:
            problems.append({'shot': entry.get('number'), 'pose': shot.get('pose'),
                             'conflicts': conflicts, 'require': rule.get('require', [])})
    return problems


def fixed_negative_tags(config=None):
    return list((config or {}).get('fixed_negative_tags', []))


def negative_tag_list(scene_negative_tags, framing_negative_tags, config=None):
    """Scene + framing negatives + fixed tail, order preserved, de-duplicated."""
    tags = [*list(scene_negative_tags), *list(framing_negative_tags),
            *fixed_negative_tags(config)]
    return list(dict.fromkeys(tags))
