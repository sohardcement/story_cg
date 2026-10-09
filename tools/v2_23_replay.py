"""V2-23 offline replay of the side-by-side pick on a copy of jobs/cg-8092d80fd20b.

Reads the original job directory only to copy it. All reads and writes after that happen in the copy
under reports/assets/V2-23/. Only the pick step is replayed: candidates, audits and ranking come from the
saved candidates.json; the side-by-side calls are real vision calls, at most 8 in total.
Run with PYTHONPATH=$PWD/src. Does not read verify/.
"""
import json
import shutil
import sys
from pathlib import Path

from storycg.candidates import pick
from storycg.config import load_config
from storycg.pairwise import resolve
from storycg.review import VisionClient
from storycg.state import save_json_atomic

ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / 'jobs/cg-8092d80fd20b'
ASSETS = ROOT / 'reports/assets/V2-23'
COPY = ASSETS / 'cg-8092d80fd20b'
MAX_CALLS = 8


def main():
    if COPY.exists():
        shutil.rmtree(COPY)
    ASSETS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(JOB, COPY)
    config = load_config(ROOT / 'config/default.json')
    store = json.loads((COPY / 'candidates.json').read_text(encoding='utf-8'))
    board = json.loads((COPY / 'storyboard.json').read_text(encoding='utf-8'))
    entries = {e['number']: e for e in board['shots']}
    vlm = VisionClient(COPY, config['model_service_url'], max_calls=MAX_CALLS, max_seconds=1500)
    rows = []
    try:
        for key in sorted(store, key=int):
            number = int(key)
            slot = store[key]
            before_selected = slot['selected']
            top, top_reason = pick(slot['candidates'])
            chosen, reason, record = resolve(COPY, number, entries[number], slot['candidates'], top, top_reason, vlm)
            slot['selected'] = chosen['candidate'] if chosen else None
            slot['reason'] = reason
            slot['pairwise'] = record
            save_json_atomic(COPY / 'candidates.json', store)
            rows.append({'shot': number, 'v21_selected': before_selected, 'pool': record['pool'],
                         'decision': record['decision'], 'selected': slot['selected'], 'reason': reason,
                         'orders': {order: {k: r.get(k) for k in ('letter_to_candidate', 'best_letter', 'best_candidate',
                                                                    'status', 'error', 'call_id', 'composite')}
                                    for order, r in record['orders'].items()}})
    finally:
        vlm.close()
    summary = {'calls_made': vlm.calls, 'max_calls': MAX_CALLS, 'rows': rows}
    save_json_atomic(ASSETS / 'replay.json', summary)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print('calls made:', vlm.calls, 'of', MAX_CALLS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
