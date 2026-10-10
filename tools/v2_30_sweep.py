"""V2-30 sensitivity: verdict changes over person_overlap values (reports only; not used to pick the value)."""
import json, sys
from pathlib import Path
ROOT = Path('/work/workspace/manga_skill/.v2')
sys.path.insert(0, str(ROOT / 'src'))
from storycg import detection_review as new  # noqa: E402

rows = [r for r in json.load(open(sys.argv[1])) if r['has_raw']]
base = new.settings()
for t in [0.3, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.97, 1.01]:
    cfg = json.loads(json.dumps(base)); cfg['count']['person_overlap'] = t
    changed = []
    for r in rows:
        shot = {'reflection': True} if r['protagonist_count']['reflection'] else {}
        n = new.protagonist_count(r['faces'], r['persons'], shot, config=cfg)
        if (n['status'], n['protagonist_count']) != (r['protagonist_count']['status'], r['protagonist_count']['protagonist_count']):
            changed.append('%s:s%dc%d:%d' % (r['job'][-6:], r['shot'], r['candidate'], n['protagonist_count']))
    print('person_overlap', t, 'changed', len(changed), changed)
