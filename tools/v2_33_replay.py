"""V2-33: replay the same-garment colour exclusion on the recorded standard outfits of 8 characters.

Input: the 7 V2-31 identity fixtures (tests/fixtures/v2_31/*/identity.json) and the Hakurei Reimu identity of
V2-32 (tests/fixtures/v2_32/identity.json). Each stored default_outfit_tags list is taken before any exclusion
(the stored records carry no same_garment_exclusion), and apply_same_garment_exclusion is run twice:
  before: colour vocabulary = the pre-V2-33 14-word list
  after:  colour vocabulary = storycg.tags.colour_words() (colors group snapshot + manual supplement)
Offline only. Prints a JSON report; nothing is written unless an output path is given.
Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_33_replay.py [OUT.json]
"""
import copy
import json
import sys
from pathlib import Path

from storycg.identity_enrichment import load_rules
from storycg.outfits import apply_same_garment_exclusion
from storycg.tags import colour_words

ROOT = Path(__file__).resolve().parents[1]
PRE_V2_33_COLOURS = ('red', 'blue', 'green', 'yellow', 'orange', 'purple', 'pink', 'white',
                     'black', 'grey', 'gray', 'brown', 'silver', 'gold')
SOURCES = sorted((ROOT / 'tests/fixtures/v2_31').glob('*/identity.json')) + [ROOT / 'tests/fixtures/v2_32/identity.json']


def _tags(identity):
    return [item['tag'] for item in identity.get('default_outfit_tags', [])]


def replay_one(path, rules, colours_after):
    identity = json.loads(Path(path).read_text(encoding='utf-8'))
    before = apply_same_garment_exclusion(copy.deepcopy(identity), rules, colours=PRE_V2_33_COLOURS)
    after = apply_same_garment_exclusion(copy.deepcopy(identity), rules, colours=colours_after)
    kept_before, kept_after = _tags(before), _tags(after)
    demoted_before = [x['tag'] for x in before.get('outfit_candidates', []) if x.get('exclusive_with')]
    demoted_after = [x['tag'] for x in after.get('outfit_candidates', []) if x.get('exclusive_with')]
    groups_after = (after.get('default_outfit_selection') or {}).get('same_garment_exclusion', {}).get('groups', [])
    return {
        'source': str(Path(path).relative_to(ROOT)),
        'character_tag': identity.get('character_tag'),
        'standard_outfit_before_count': len(_tags(identity)),
        'kept_before': kept_before,
        'kept_after': kept_after,
        'demoted_before': demoted_before,
        'demoted_after': demoted_after,
        'newly_demoted': [t for t in demoted_after if t not in demoted_before],
        'no_longer_demoted': [t for t in demoted_before if t not in demoted_after],
        'groups_after': groups_after,
    }


def main(argv):
    rules = load_rules()
    colours_after = colour_words()
    report = {'colour_words_after': list(colours_after),
              'colour_words_before': list(PRE_V2_33_COLOURS),
              'characters': [replay_one(path, rules, colours_after) for path in SOURCES]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if argv:
        out = Path(argv[0])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
