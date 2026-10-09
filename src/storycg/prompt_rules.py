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
    """Handwritten supplement plus Danbooru group membership (V2-09)."""
    from storycg.scene_groups import non_object_group_tags
    return ({canonical_tag(tag) for tag in rules.get('non_object_tags', [])}
            | set(non_object_group_tags(rules)))


def _action_invoking_set(rules):
    return {canonical_tag(tag) for tag in (rules.get('action_invoking_props') or {})}


def indoor_precipitation(scene_tags, rules):
    """True when the scene is indoors (indoors tag or Indoors-section location) and has a precipitation tag (V2-11)."""
    from storycg.scene_groups import indoor_location_tags
    present = {canonical_tag(tag) for tag in scene_tags}
    indoor = set(indoor_location_tags(rules)) | {'indoors'}
    precipitation = {canonical_tag(tag) for tag in rules.get('indoor_precipitation_tags', [])}
    return bool(present & indoor) and bool(present & precipitation)


def indoor_precipitation_negatives(scene_tags, positive_tags, negative_tags, config=None):
    """V2-18 (D60): ground-water negatives for an indoor precipitation shot.

    The trigger is the V2-11 rule itself (indoor_precipitation), so the window and this rule agree.
    A tag is skipped, with a reason, when the positive prompt already has it or a listed synonym, or
    when the negative prompt already has it. Returns the flag, the added tags and the skipped tags.
    """
    cfg = (config or {}).get('indoor_precipitation_negative') or {}
    result = {'indoor_precipitation': False, 'added': [], 'skipped': []}
    if not cfg or not indoor_precipitation(scene_tags, scene_rules(config)):
        return result
    result['indoor_precipitation'] = True
    positive = {canonical_tag(token) for token in str(positive_tags).split(',') if token.strip()}
    negative = {canonical_tag(token) for token in negative_tags}
    for tag in cfg['tags']:
        canon = canonical_tag(tag)
        synonyms = [canonical_tag(s) for s in cfg.get('synonyms', {}).get(tag, [])]
        present = [s for s in [canon, *synonyms] if s in positive]
        if present:
            note = '' if present[0] == canon else '（同义的地面湿 tag）'
            result['skipped'].append({'tag': tag, 'reason': f'正向已有 {present[0]}{note}'})
        elif canon in negative:
            result['skipped'].append({'tag': tag, 'reason': '负向已有该 tag'})
        else:
            result['added'].append(tag)
    return result


def filter_scene_tags(framing, scene_tags, prop_tags, config=None):
    """Return (kept, dropped) scene tags under the framing's object budget.

    Non-object tags (setting/place/time/weather/light) never count. Objects are
    dropped beyond the budget; action-invoking objects are dropped outright
    unless they are the shot's one declared prop tag.
    """
    rules = scene_rules(config)
    limit = _object_limit(framing, rules)
    non_objects = _non_object_set(rules)
    # V2-11: the window added for indoor precipitation is never counted (and kept in closeups).
    # Only that case: a planner-written window elsewhere still takes a slot, as before.
    if indoor_precipitation(scene_tags, rules):
        non_objects = non_objects | {canonical_tag(tag) for tag in rules.get('budget_exempt_tags', [])}
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
    if framing == 'closeup':
        # V2-11: a closeup keeps its one declared prop (the object cap of 0 still covers every other object).
        for tag, value in entries:
            if tag not in keep and value in declared and value not in non_objects:
                keep.add(tag)
                break
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


def companion_triggers(shot):
    """Canonical keys from pose, expression and tags alike (V2-08)."""
    keys = []
    sources = [shot.get('pose'), shot.get('expression'), *(shot.get('tags') or [])]
    for key in sources:
        if isinstance(key, str) and key:
            keys.append(canonical_tag(key))
    return list(dict.fromkeys(keys))


def companion_tags_for_shot(shot, config=None):
    """Companion tags the shot's action implies, e.g. sleeping -> closed eyes."""
    table = companion_rules(config)
    required = []
    for key in companion_triggers(shot):
        rule = table.get(key)
        if rule:
            required.extend(rule.get('require', []))
    return list(dict.fromkeys(required))


def companion_conflicts(board, config=None):
    """Shots whose written pose/expression/tags contradict a companion rule."""
    table = companion_rules(config)
    problems = []
    for entry in board.get('shots', []):
        shot = entry.get('shot', {})
        triggers = companion_triggers(shot)
        rules = [(key, table[key]) for key in triggers if key in table]
        if not rules:
            continue
        written = set(triggers)
        for key, rule in rules:
            conflicts = sorted({canonical_tag(c) for c in rule.get('conflicts', [])} & written)
            if conflicts:
                problems.append({'shot': entry.get('number'), 'trigger': key,
                                 'conflicts': conflicts, 'require': rule.get('require', [])})
    return problems


def fixed_negative_tags(config=None):
    return list((config or {}).get('fixed_negative_tags', []))


def negative_tag_list(scene_negative_tags, framing_negative_tags, config=None):
    """Scene + framing negatives + fixed tail, order preserved, de-duplicated."""
    tags = [*list(scene_negative_tags), *list(framing_negative_tags),
            *fixed_negative_tags(config)]
    return list(dict.fromkeys(tags))
