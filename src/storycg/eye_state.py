"""Eye state/action vocabulary derived from the cached Danbooru eyes tag group.

The snapshot (cache/tag_groups/eyes.json) is the sole source for tag
membership, mirroring posture_tags.py: the wiki body's bullet links are the
candidate tags and config/default.json `eye_state_tags.roots` selects the
state/action subset (eye openness states and actions performed on the eyes).
Every root is verified against the snapshot at load; color, shape and size
tags are not roots and stay appearance-forbidden.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from storycg.tags import canonical_tag

CACHE = Path(__file__).resolve().parents[2] / 'cache/tag_groups'


def extract_tags(body):
    tags = set()
    for line in body.splitlines():
        if not re.match(r'^\*+\s+\[\[', line):
            continue
        for link in re.findall(r'\[\[([^\]]+)\]\]', line):
            tag = canonical_tag(link.split('|', 1)[0])
            if ':' not in tag:
                tags.add(tag)
    return tags


@lru_cache(maxsize=1)
def eyes_snapshot_tags():
    raw = (CACHE / 'eyes.json').read_bytes()
    provenance = json.loads((CACHE / 'eyes.provenance.json').read_text())
    if hashlib.sha256(raw).hexdigest() != provenance['sha256']:
        raise ValueError('eyes source cache checksum mismatch')
    page = json.loads(raw)
    if page.get('title') != 'tag_group:eyes_tags':
        raise ValueError('incorrect eyes wiki source')
    tags = extract_tags(page['body'])
    if not tags:
        raise ValueError('eyes source has no tag bullets')
    return frozenset(tags)


def _configured_roots(config):
    if config is None:
        from storycg.config import load_config
        config = load_config('config/default.json')
    return [canonical_tag(root) for root in (config.get('eye_state_tags') or {}).get('roots', [])]


def eye_state_tags(config=None):
    """Configured state/action tags, each verified present in the snapshot."""
    snapshot = eyes_snapshot_tags()
    roots = _configured_roots(config)
    missing = [root for root in roots if root not in snapshot]
    if missing:
        raise ValueError('eye state roots not in tag_group:eyes_tags: ' + ', '.join(missing))
    return roots


@lru_cache(maxsize=1)
def _eye_state_words_cached():
    return frozenset(word for tag in eye_state_tags(None) for word in re.split(r'[^a-z0-9]+', tag) if word)


def eye_state_words(config=None):
    """Words of the state/action tags, e.g. closed, half, blinking, one."""
    if config is None:
        return _eye_state_words_cached()
    return frozenset(word for tag in eye_state_tags(config) for word in re.split(r'[^a-z0-9]+', tag) if word)
