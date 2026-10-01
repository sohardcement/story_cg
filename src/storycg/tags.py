"""Exact Danbooru tag existence checks, with persisted positive/negative responses."""
from pathlib import Path
import re
import httpx
from storycg.identity import BASE_URL, _CachedDanbooru


def canonical_tag(value):
    return re.sub(r'\s+', '_', value.strip().lower())


def _split_prop_attribute(tag, kind, registry, path):
    """Recover only an object-bound attribute, checking both exact components."""
    normalized = canonical_tag(tag)
    match = re.match(
        r'^((?:(?:light|dark|pale|bright|golden)_)?(?:red|blue|green|yellow|orange|purple|pink|white|black|grey|gray|brown|silver|gold|multicolored|rainbow|striped|plaid|checkered|polka_dot|floral|argyle|camouflage|patterned))_(.+)$',
        normalized)
    if not match:
        return None
    attribute, obj = match.groups()
    kind = canonical_tag(kind)
    if obj != kind and not obj.endswith('_' + kind):
        return None
    valid = []
    for component in (attribute, obj):
        valid.append(registry.exists(component))
        registry.records[-1].update(field=path, action='split_component_checked',
                                    compound=normalized)
    return [attribute, obj] if all(valid) else None


class TagRegistry:
    def __init__(self, cache_dir, *, client):
        self.source = _CachedDanbooru(Path(cache_dir), client)
        self.records = []

    def exists(self, value):
        tag = canonical_tag(value)
        payload, url = self.source.get('/tags.json', {'search[name]': tag, 'limit': 10})
        found = next((item for item in payload if item.get('name') == tag and
                      not item.get('is_deprecated', False)), None)
        self.records.append({'input': value, 'tag': tag, 'exists': found is not None,
                             'source': url, 'category': found.get('category') if found else None})
        return found is not None


def validate_tag_lists(board, registry, config=None, *, drop_unknown=False):
    lists = []
    prop_kinds = {}
    for prop in board['props']:
        path = f"props.{prop['id']}.description_tags"
        prop_kinds[path] = prop['kind']
        lists.append((path, prop['description_tags']))
        lists.append((f"props.{prop['id']}.kind", [prop['kind']]))
    for entry in board['shots']:
        n = entry['number']
        for group, field in [('scene', 'tags'), ('scene', 'negative_tags'), ('outfit', 'tags'), ('shot', 'tags')]:
            lists.append((f'{n}.{group}.{field}', entry[group][field]))
        for ref in entry['shot']['key_props']:
            for field in ('relation_tags', 'location_tags'):
                lists.append((f"{n}.props.{ref['prop_id']}.{field}", ref[field]))
    if config:
        for mapping in ('framing_tags', 'camera_tags', 'framing_negative_tags'):
            for key, tags in config.get(mapping, {}).items():
                lists.append((f'config.{mapping}.{key}', tags))
    missing = []
    pose_feedback = []
    for path, tags in lists:
        for tag in list(tags):
            if not registry.exists(tag):
                record = registry.records[-1]
                record['field'] = path
                components = (_split_prop_attribute(tag, prop_kinds[path], registry, path)
                              if path in prop_kinds else None)
                if components:
                    index = tags.index(tag)
                    tags.remove(tag)
                    for component in components:
                        if component not in tags:
                            tags.insert(index, component)
                            index += 1
                    record.update(action='split_verified', replacement=components)
                    continue
                if drop_unknown and not path.startswith('config.') and not path.endswith('.kind'):
                    tags.remove(tag)
                    record['action'] = 'discarded'
                    # A discarded conditioning pose is not recoverable by generic
                    # single-moment feedback. Preserve its field and real alternatives.
                    if path.endswith('.shot.tags'):
                        entry = next(e for e in board['shots'] if str(e['number']) == path.split('.')[0])
                        if canonical_tag(tag) == canonical_tag(entry['shot'].get('pose', '')):
                            candidates, evidence = pose_candidates(tag, registry)
                            field = f"$.shots[{board['shots'].index(entry)}].shot.pose"
                            record.update(pose_field=field, candidates=candidates, candidate_evidence=evidence)
                            pose_feedback.append(f"{field}: {tag}: 该 tag 在 Danbooru 不存在; "
                                f"verified candidates: {', '.join(candidates) or 'none available'}; "
                                "select one real instantaneous pose; action/description/shot.tags are derived from pose")
                else:
                    record['action'] = 'rewrite_required'
                    missing.append(f'{path}: {tag}')
            else:
                registry.records[-1]['action'] = 'accepted'
                registry.records[-1]['field'] = path
    if pose_feedback:
        from storycg.story_rules import StoryRuleViolation
        raise StoryRuleViolation('single_moment', [r['pose_field'] for r in registry.records if r.get('pose_field')],
            '; '.join(pose_feedback), 'replace the nonexistent pose with a verified candidate; do not repeat the rejected tag')
    if missing:
        raise ValueError('nonexistent Danbooru tags; rewrite these: ' + '; '.join(missing))


def pose_candidates(value, registry, limit=5):
    """Rank sourced group members, then verify each through the exact tag API."""
    from difflib import SequenceMatcher
    from storycg.posture_tags import posture_tags
    tag = canonical_tag(value)
    tokens = set(tag.split('_'))
    def rank(candidate):
        overlap = len(tokens & set(candidate.split('_'))) / max(1, len(tokens))
        return overlap + SequenceMatcher(None, tag, candidate).ratio()
    ranked = sorted(posture_tags(), key=lambda t: (-rank(t), t))
    found, evidence = [], []
    for candidate in ranked[:max(limit * 3, limit)]:
        if registry.exists(candidate):
            found.append(candidate)
            evidence.append(dict(registry.records[-1]))
        registry.records[-1].update(action='pose_candidate_checked', for_tag=tag)
        if len(found) == limit:break
    return found, evidence
