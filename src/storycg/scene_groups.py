"""Scene-environment tag membership from cached Danbooru group wiki snapshots.

Snapshots live in cache/tag_groups/<group>.json with provenance records
(<group>.provenance.json), mirroring eye_state.py / posture_tags.py: config
names the groups and carries each source URL plus sha256, and every load
re-verifies checksum and wiki title before the tags are used.

Extraction is stricter than the eyes/posture bullet scan: only the leading
[[..]] links of a bullet are tags. Links inside the trailing prose (after a
':' or '(' outside a link) are descriptions, e.g. the lighting page's
"[[refraction]]: ... through a [[glass]] of [[water]]" must not make glass a
non-object. Tags containing ':' (wiki namespaces) are skipped like elsewhere.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

CACHE = Path(__file__).resolve().parents[2] / 'cache/tag_groups'

_BULLET = re.compile(r'^\*+\s+')
_ASSET_REF = re.compile(r'^\*+\s+(?:![a-z]+\s+#\d+:)\s*')


def extract_tags(body):
    tags = set()
    for line in body.splitlines():
        if not _BULLET.match(line):
            continue
        content = _ASSET_REF.sub('', line)
        links, buf, in_link, i = [], '', False, 0
        while i < len(content):
            if content.startswith('[[', i):
                in_link, buf, i = True, '', i + 2
            elif in_link and content.startswith(']]', i):
                links.append(buf)
                in_link, buf, i = False, '', i + 2
            elif in_link:
                buf += content[i]
                i += 1
            elif content[i] in ':(':
                break  # trailing description; only leading links are tags
            else:
                i += 1
        for link in links:
            tag = re.sub(r'\s+', '_', link.split('|', 1)[0].strip().lower())
            if tag and ':' not in tag:
                tags.add(tag)
    return tags


@lru_cache(maxsize=None)
def _snapshot_tags(name, wiki_title, sha256):
    raw = (CACHE / f'{name}.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError(f'{name} source cache checksum mismatch')
    page = json.loads(raw)
    if page.get('title') != wiki_title:
        raise ValueError(f'incorrect {name} wiki source')
    tags = extract_tags(page['body'])
    if not tags:
        raise ValueError(f'{name} source has no tag bullets')
    return frozenset(tags)


def non_object_group_tags(rules):
    """Union of the scene_rules-configured group snapshots' tags."""
    union = set()
    for name, meta in ((rules or {}).get('non_object_tag_groups') or {}).items():
        union |= _snapshot_tags(name, meta['wiki_title'], meta['sha256'])
    return frozenset(union)
