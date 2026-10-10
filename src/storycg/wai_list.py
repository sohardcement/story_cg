"""V2-25 (TASK-v2 §10.4, D67/D68): the WAI character list, a third-party tag list (MIT).

Two uses, both switchable by config `wai_list.enabled` (default on):
- name lookup: a Chinese name that hits exactly one row of the list is resolved to the list's English tag,
  then to the real Danbooru character tag by an autocomplete query (see identity.resolve_identity);
- tier: a character whose tag is in the list is L2 without a probe image (tier_gate.ensure_identity_tier).

The list is written as `name（work）,tag` with spaces in the tag and no colons; it is not a Danbooru tag,
so every use goes through `normalize` and a Danbooru match. The file's sha256 is checked against the
manifest on every load; a mismatch raises rather than silently using the file.
"""
import hashlib
import json
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = 'data/wai/waiIllustriousSDXL_v160_characters.csv'
DEFAULT_MANIFEST = 'data/wai/manifest.json'
NAME_SPLIT = re.compile(r'[（(]')


class WaiListError(ValueError):
    """The list file is missing, or its bytes do not match the manifest."""


def normalize(tag):
    """Lower case; underscores, colons and runs of spaces all become one space. So `2b_(nier:automata)`,
    `2b (nier automata)` and `2B (Nier Automata)` compare equal. (A colon is a space, not nothing:
    `re:zero` must match the list's `re zero`.)"""
    text = str(tag).strip().lower().replace(':', ' ').replace('_', ' ')
    return ' '.join(text.split())


def settings_from(config=None):
    """Settings of a whole config (the default config when None)."""
    if config is None:
        config_path = PROJECT / 'config' / 'default.json'
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    return settings_for(config.get('wai_list'))


def settings_for(block=None):
    """Settings from a `wai_list` block (None: defaults). Defaults: enabled, the bundled files."""
    block = dict(block or {})
    return {'enabled': bool(block.get('enabled', True)),
            'csv': str((PROJECT / block.get('csv', DEFAULT_CSV)).resolve()),
            'manifest': str((PROJECT / block.get('manifest', DEFAULT_MANIFEST)).resolve())}


def enabled(config):
    return settings_from(config)['enabled']


def load(settings):
    """Rows of the list after the sha256 check: {'rows': [{'cn','name','tag','line'}], 'sha256', 'manifest'}."""
    csv_path, manifest_path = Path(settings['csv']), Path(settings['manifest'])
    try:
        raw = csv_path.read_bytes()
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise WaiListError(f'WAI 清单文件不可读：{exc}') from exc
    digest = hashlib.sha256(raw).hexdigest()
    expected = manifest.get('csv', {}).get('sha256')
    if digest != expected:
        raise WaiListError(f'WAI 清单 sha256 不符：文件 {digest}，清单记录 {expected}；拒绝使用')
    rows = []
    for line in raw.decode('utf-8-sig').splitlines():
        if not line.strip():
            continue
        cn, tag = line.rsplit(',', 1)  # tags never contain commas
        cn, tag = cn.strip(), tag.strip()
        rows.append({'cn': cn, 'name': NAME_SPLIT.split(cn, maxsplit=1)[0], 'tag': tag, 'line': line})
    return {'rows': rows, 'sha256': digest, 'manifest': manifest}


def find_name(rows, query):
    """Rows for a Chinese name. A full-label match (the whole `cn`) is preferred; otherwise the name before
    the first bracket. Returns (rows, how) where how is 'full_label', 'name', 'ambiguous' or 'none'."""
    query = query.strip()
    full = [r for r in rows if r['cn'] == query]
    if len(full) == 1:
        return full, 'full_label'
    if len(full) > 1:
        return full, 'ambiguous'
    by_name = [r for r in rows if r['name'] == query]
    if len(by_name) == 1:
        return by_name, 'name'
    return by_name, ('ambiguous' if len(by_name) > 1 else 'none')


def evidence(loaded, row, settings):
    return {'source': 'wai_list', 'file': str(Path(settings['csv']).relative_to(PROJECT)),
            'sha256': loaded['sha256'], 'list_rows': len(loaded['rows']),
            'manifest_note': '清单对应 WAI Illustrious v16，项目底模是 v17',
            'matched_line': row['line'], 'list_tag': row['tag'], 'list_name': row['cn']}


def lookup_name(name, settings):
    """Evidence dict for a name that hits exactly one list row, else None (the caller falls back)."""
    if not settings['enabled']:
        return None
    loaded = load(settings)
    rows, how = find_name(loaded['rows'], name)
    if how not in ('full_label', 'name'):
        return None
    return {**evidence(loaded, rows[0], settings), 'match': how}


def tier_row(tag, settings):
    """The first list row whose tag equals `tag` after normalization, else None."""
    if not settings['enabled']:
        return None
    loaded = load(settings)
    target = normalize(tag)
    for row in loaded['rows']:
        if normalize(row['tag']) == target:
            return evidence(loaded, row, settings)
    return None
