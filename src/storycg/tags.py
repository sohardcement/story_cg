"""Exact Danbooru tag existence checks, with persisted positive/negative responses."""
from pathlib import Path
import re
import httpx
from storycg.identity import BASE_URL, _CachedDanbooru


def canonical_tag(value):
    return re.sub(r'\s+', '_', value.strip().lower())


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
    for prop in board['props']:
        lists.append((f"props.{prop['id']}.description_tags", prop['description_tags']))
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
    for path, tags in lists:
        for tag in list(tags):
            if not registry.exists(tag):
                record = registry.records[-1]
                record['field'] = path
                if drop_unknown and not path.startswith('config.') and not path.endswith('.kind'):
                    tags.remove(tag)
                    record['action'] = 'discarded'
                else:
                    record['action'] = 'rewrite_required'
                    missing.append(f'{path}: {tag}')
            else:
                registry.records[-1]['action'] = 'accepted'
                registry.records[-1]['field'] = path
    if missing:
        raise ValueError('nonexistent Danbooru tags; rewrite these: ' + '; '.join(missing))
