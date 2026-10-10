"""V2-30 diagnosis: dump detection boxes of every candidate audit record (read-only)."""
import json, sys
from pathlib import Path

JOBS = [
    ('/work/workspace/manga_skill/.v2/jobs/cg-3cd4d7bfd1ae', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-8092d80fd20b', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-b6dcbc8f995f', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-baf19a902b01', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-d86de5dc9b1a', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-778717bdaa05', 'v2'),
    ('/work/workspace/manga_skill/.v2/jobs/cg-cd6ecc9fc154', 'v2'),
    ('/work/workspace/manga_skill/jobs/cg-469f0a9b0f01', 'mainline'),
]

def main(out):
    rows = []
    for job, where in JOBS:
        job = Path(job)
        cand = json.loads((job / 'candidates.json').read_text())
        for shot, block in sorted(cand.items(), key=lambda kv: int(kv[0])):
            for c in block['candidates']:
                label = c.get('audit_label')
                art_path = job / 'cycle_artifacts' / f'{label}.json'
                rec = {'job': job.name, 'where': where, 'shot': int(shot), 'candidate': c['candidate'],
                       'status': c.get('status'), 'conclusion': c.get('conclusion'),
                       'screen': c.get('screen', []), 'image': c.get('image', {}).get('path'),
                       'audit_label': label}
                if art_path.exists():
                    art = json.loads(art_path.read_text())
                    det = art.get('detection_items', {})
                    pc = det.get('protagonist_count', {})
                    rec['protagonist_count'] = {k: pc.get(k) for k in
                        ('status', 'protagonist_count', 'allowed_count', 'reflection', 'major_face_indices',
                         'major_person_indices', 'merged_person_indices')}
                    rec['thresholds'] = pc.get('thresholds')
                    raw = det.get('detection', {})
                    rec['size'] = raw.get('size')
                    rec['faces'] = [{'bbox': f['bbox'], 'confidence': f['confidence'], 'area_ratio': f['area_ratio']} for f in raw.get('faces', [])]
                    rec['persons'] = [{'bbox': p['bbox'], 'confidence': p['confidence'], 'area_ratio': p['area_ratio'],
                                       'mask_area_ratio': p.get('mask_area_ratio')} for p in raw.get('persons', [])]
                    rec['has_raw'] = True
                else:
                    rec['has_raw'] = False
                rows.append(rec)
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(len(rows), 'candidate rows')

if __name__ == '__main__':
    main(sys.argv[1])
