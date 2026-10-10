"""V2-25 offline replay: `storycg compile` with a placeholder beats file for the holdout names.

Reads Danbooru and ComfyUI /history only (GET). The placeholder is invalid on purpose: compile stops after
identity and tier, which is the part under test. Writes reports/assets/V2-25/replay.json.
Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_25_replay.py
"""
import contextlib
import io
import json
from pathlib import Path

import httpx

from storycg import agent
from storycg.config import load_config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports' / 'assets' / 'V2-25'
NAMES = ['雷姆', '2B', '初音未来', '芙莉莲']


def history_count(url):
    with httpx.Client(trust_env=False, timeout=30) as client:
        response = client.get(url.rstrip('/') + '/history')
        response.raise_for_status()
        return len(response.json())


def scratch_dirs():
    base = ROOT / 'compile'
    return {p.name for p in base.iterdir()} if base.exists() else set()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    comfy = load_config(ROOT / 'config' / 'default.json')['comfyui_url']
    placeholder = OUT / 'placeholder_beats.json'
    before = history_count(comfy)
    runs = []
    for name in NAMES:
        known = scratch_dirs()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = agent.beats_cli(['compile', '--character', name, '--beats', str(placeholder)])
        output = json.loads(buffer.getvalue())
        created = sorted(scratch_dirs() - known)
        identity = json.loads((ROOT / 'compile' / created[-1] / 'identity.json').read_text(encoding='utf-8')) if created else {}
        wai = identity.get('wai_list') or {}
        runs.append({'input': name, 'exit': code, 'compile_status': output.get('status'),
                     'status': identity.get('status'), 'character_tag': identity.get('character_tag'),
                     'identity_tier': identity.get('identity_tier'),
                     'tier_evidence_status': (identity.get('tier_evidence') or {}).get('status'),
                     'wai_list': {k: wai.get(k) for k in ('match', 'list_name', 'list_tag', 'danbooru_tag', 'list_rows',
                                                          'sha256', 'matched_line', 'manifest_note')} or None,
                     'scratch_dir': f'compile/{created[-1]}' if created else None})
    after = history_count(comfy)
    result = {'comfyui_url': comfy, 'history_before': before, 'history_after': after, 'runs': runs}
    (OUT / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
