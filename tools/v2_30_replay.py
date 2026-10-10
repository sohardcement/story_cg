"""V2-30 replay: old (HEAD) vs new protagonist_count on every stored candidate (read-only on jobs)."""
import importlib.util, json, sys
from pathlib import Path

ROOT = Path('/work/workspace/manga_skill/.v2')
OLD_DIR = ROOT / '.pytest-tmp/v2-30/old'
sys.path.insert(0, str(ROOT / 'src'))
from storycg import detection_review as new  # noqa: E402

spec = importlib.util.spec_from_file_location('old_detection_review', OLD_DIR / 'old_detection_review.py')
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
old_cfg = json.loads((OLD_DIR / 'old_detection_review.json').read_text())
new_cfg = new.settings()

def verdict(res):
    return {'status': res['status'], 'count': res['protagonist_count'],
            'persons': res['major_person_indices'], 'faces': res['major_face_indices'],
            'groups': res.get('duplicate_person_groups', [])}

def main(src, out):
    rows = json.load(open(src))
    report = []
    for r in rows:
        if not r['has_raw']:
            continue
        shot = {'reflection': True} if r['protagonist_count']['reflection'] else {}
        o = old.protagonist_count(r['faces'], r['persons'], shot, config=old_cfg)
        n = new.protagonist_count(r['faces'], r['persons'], shot, config=new_cfg)
        stored = r['protagonist_count']['status']
        report.append({'job': r['job'], 'where': r['where'], 'shot': r['shot'], 'candidate': r['candidate'],
                       'screened_by_count': any(s.startswith('人数检测不过') for s in r['screen']),
                       'stored_status': stored, 'stored_count': r['protagonist_count']['protagonist_count'],
                       'old_matches_stored': o['status'] == stored and o['protagonist_count'] == r['protagonist_count']['protagonist_count'],
                       'old': verdict(o), 'new': verdict(n),
                       'changed': (o['status'], o['protagonist_count']) != (n['status'], n['protagonist_count'])})
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print('rows', len(report), 'old_matches_stored', sum(x['old_matches_stored'] for x in report))
    for x in report:
        if x['screened_by_count'] or x['changed']:
            print(x['job'][-6:], x['where'][:3], 's%d c%d' % (x['shot'], x['candidate']),
                  'screened' if x['screened_by_count'] else 'NOT-screened',
                  'old', x['old']['status'], x['old']['count'], '-> new', x['new']['status'], x['new']['count'],
                  'groups', x['new']['groups'])

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
