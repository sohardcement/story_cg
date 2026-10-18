"""D116: footwear and legwear tags. A closeup or medium shot drops them from the outfit block: a tag such as `black shoes` pulls the feet into the
frame and the shot is drawn full body (D115). The list is config/leg_tags.json; a tag matches by its words (`white_socks` has the word `socks`)
or by its whole name."""
import json
import re
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'config' / 'leg_tags.json'


def _load():
    data = json.loads(PATH.read_text(encoding='utf-8'))
    return set(data['framings']), set(data['tokens']), {t.replace('_', ' ') for t in data['tags']}


def applies_to(framing):
    return framing in _load()[0]


def is_leg_tag(tag):
    _, tokens, names = _load()
    text = str(tag).strip().lower().replace('_', ' ')
    return text in names or bool(set(re.split(r'[\s-]+', text)) & tokens)
