"""Exact Danbooru tag existence checks, with persisted positive/negative responses."""
from pathlib import Path
import re
import httpx
from storycg.identity import BASE_URL, _CachedDanbooru


def canonical_tag(value):
    return re.sub(r'\s+', '_', value.strip().lower())


# Colour vocabulary shared by identity exclusion rules (V2-31). Shade words qualify a colour.
COLOR_WORDS = ('red', 'blue', 'green', 'yellow', 'orange', 'purple', 'pink', 'white',
               'black', 'grey', 'gray', 'brown', 'silver', 'gold')
SHADE_WORDS = ('light', 'dark', 'pale', 'bright', 'golden')
_COLOR_PREFIX = re.compile(rf"^(?:(?:{'|'.join(SHADE_WORDS)})_)?({'|'.join(COLOR_WORDS)})_(.+)$")


def color_split(tag):
    """Return (colour, garment noun) for a colour-prefixed tag such as brown_shoes, else None."""
    match = _COLOR_PREFIX.match(canonical_tag(tag))
    return (match.group(1), match.group(2)) if match else None

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
        if 'framing' in config:
            for key, rule in config['framing'].items():
                lists.append((f'config.framing.{key}.tags', rule['tags']))
        for mapping in (('framing_tags',) if 'framing' not in config else ()) + ('camera_tags', 'framing_negative_tags'):
            for key, tags in config.get(mapping, {}).items():
                lists.append((f'config.{mapping}.{key}', tags))
    missing = []
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
                    # Resolve against the current state, never ask the planner to
                    # replace the action with a merely similar body-part tag.
                    if path.endswith('.shot.tags'):
                        entry = next(e for e in board['shots'] if str(e['number']) == path.split('.')[0])
                        if canonical_tag(tag) == canonical_tag(entry['shot'].get('pose', '')):
                            shot = entry['shot']
                            context = {key: shot.get(key, '') for key in
                                       ('action', 'spatial_relation', 'change_from_previous')}
                            candidates, evidence = pose_candidates(tag, registry, context=context)
                            field = f"$.shots[{board['shots'].index(entry)}].shot.pose"
                            record.update(pose_field=field, candidates=candidates,
                                          candidate_evidence=evidence, semantic_context=context)
                            if candidates:
                                replacement = candidates[0]
                                shot.update(pose=replacement, action=replacement, description=replacement)
                                shot.pop('pose_tag_omitted', None)
                                tags.insert(0, replacement)
                                record.update(action='pose_replaced', replacement=replacement,
                                              reason='complete action tokens supported by every relevant context field')
                            else:
                                shot['pose_tag_omitted'] = True
                                record.update(action='pose_omitted', replacement=None,
                                              reason='no verified candidate preserves the complete action and spatial/change context')
                else:
                    record['action'] = 'rewrite_required'
                    missing.append(f'{path}: {tag}')
            else:
                registry.records[-1]['action'] = 'accepted'
                registry.records[-1]['field'] = path
    if missing:
        raise ValueError('nonexistent Danbooru tags; rewrite these: ' + '; '.join(missing))


def pose_candidates(value, registry, limit=5, *, context=None):
    """Conservative lexical proof, followed by exact API verification.

    Require the complete action vocabulary, rather than partial body-part or
    string similarity. Any prose field mentioning that action must support all
    its tokens. Unsupported paraphrases are deliberately omitted.
    """
    from storycg.posture_tags import posture_tags
    tag = canonical_tag(value)
    def tokens(text):
        return set(re.findall(r'[a-z0-9]+', text.lower().replace('_', ' ')))
    requested = tokens(tag)
    def compatible(candidate):
        required = tokens(candidate)
        if required != requested:
            return False
        # Prepositions bind actor to target. A bag of words cannot establish
        # their direction, so never reorder a relational pose to invent one.
        relations = {'on','over','behind','under','in','at','to','from','against',
                     'below','above','beneath','between','around','through'}
        if required & relations and candidate != tag:
            return False
        for text in (context or {}).values():
            words = tokens(text)
            if required & words:
                if not required <= words or words & {'not', 'never', 'without', 'no'}:
                    return False
        return True
    ranked = sorted(t for t in posture_tags() if compatible(t))
    found, evidence = [], []
    for candidate in ranked:
        if registry.exists(candidate):
            found.append(candidate)
            evidence.append(dict(registry.records[-1]))
        registry.records[-1].update(action='pose_candidate_checked', for_tag=tag)
        if len(found) == limit:break
    return found, evidence
