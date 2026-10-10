"""V2-31 replay: re-resolve the seven fixture identities with the current rules.

Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_31_replay.py <stage-name>
Reads tests/fixtures/v2_31/*/identity.json (input_name + character_tag) and the Danbooru
cache under cache/danbooru; network is used only for cache misses. No generation, no models.
"""
import json
import sys
from pathlib import Path

import httpx

from storycg.identity import BASE_URL, USER_AGENT, resolve_identity

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'v2_31'
CACHE = ROOT / 'cache' / 'danbooru'


def summary(identity):
    def tags(items):
        return [item['tag'] for item in items or []]
    return {
        'status': identity.get('status'),
        'appearance_locked': tags(identity.get('appearance_tags')),
        'outfit_locked': tags(identity.get('default_outfit_tags')),
        'outfit_alternates': [tags(e.get('tags')) for e in identity.get('alternate_outfits', [])],
        'outfit_candidates': tags(identity.get('outfit_candidates')),
        'identity_conflicts': [{k: c.get(k) for k in ('category', 'tags', 'decision', 'selected')}
                               for c in identity.get('identity_conflicts', [])],
        'identity_block': identity.get('identity_block'),
        'outfit_block': identity.get('outfit_block'),
    }


def main(stage):
    out = {}
    with httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False,
                      headers={'User-Agent': USER_AGENT}) as client:
        for path in sorted(FIXTURES.glob('*/identity.json')):
            stored = json.loads(path.read_text(encoding='utf-8'))
            fresh = resolve_identity(stored['input_name'], CACHE, client=client,
                                     character_tag=stored['character_tag'])
            out[path.parent.name] = {'character_tag': stored['character_tag'],
                                     'stored': summary(stored), 'replay': summary(fresh)}
    target = ROOT / 'reports' / 'assets' / 'V2-31' / f'replay-{stage}.json'
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(target)


if __name__ == '__main__':
    main(sys.argv[1])
