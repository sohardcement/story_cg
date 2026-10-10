"""V2-30 diagnosis: pairwise IoU / containment among person and face boxes (read-only)."""
import json, sys

def area(b):
    return max(0, b[2]-b[0]) * max(0, b[3]-b[1])

def inter(a, b):
    return area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])

def iou(a, b):
    i = inter(a, b); u = area(a) + area(b) - i
    return i / u if u else 0.0

def contain(a, b):
    """Fraction of the SMALLER box lying inside the larger one (intersection / smaller area)."""
    i = inter(a, b); s = min(area(a), area(b))
    return i / s if s else 0.0

def pairs(rows):
    out = []
    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            a, b = rows[i]['bbox'], rows[j]['bbox']
            out.append({'i': i, 'j': j, 'iou': round(iou(a, b), 4), 'contain_small': round(contain(a, b), 4),
                        'centre_dist_over_diag': None})
    return out

def main(src, out):
    rows = json.load(open(src))
    res = []
    for r in rows:
        if not r['has_raw']:
            continue
        r['person_pairs'] = pairs(r['persons'])
        r['face_pairs'] = pairs(r['faces'])
        # face-to-person: is each face inside a person box?
        fp = []
        for fi, f in enumerate(r['faces']):
            for pi, p in enumerate(r['persons']):
                fp.append({'face': fi, 'person': pi, 'face_in_person': round(inter(f['bbox'], p['bbox'])/max(area(f['bbox']),1e-9), 4)})
        r['face_person'] = fp
        res.append(r)
    json.dump(res, open(out, 'w'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
