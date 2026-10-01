"""Fixed item mappings and fail-only calibration, with explicit cohort provenance."""
from collections import Counter
from storycg.calibration import ITEMS, mapped_status
from storycg.detection_review import DETECTION_KEYS

VISUAL_ITEMS = (*ITEMS, 'hair_ornaments', 'footwear')
ALL_ITEMS = (*VISUAL_ITEMS, *DETECTION_KEYS)


def map_visual(item, single, consistency, *, cohort):
    if item == 'footwear':
        if consistency['status'] != 'pass':
            return 'fail'
        keys = [key for key, value in consistency.get('checklist', {}).items()
                if key.startswith('outfit_component_') and any(
                    word in str(value['expected']).lower().replace('_', ' ').split()
                    for word in ('boots', 'shoes', 'sandals', 'sneakers', 'footwear'))]
        statuses = [consistency['data'][key]['status'] for key in keys]
        return aggregate(statuses)
    if item == 'hair_ornaments' or (item == 'accessories' and cohort == 'v2'):
        return consistency.get('data', {}).get(item, {}).get('status', 'fail')
    return mapped_status(item, single, consistency)


def aggregate(statuses):
    for status in ('fail', 'minor', 'not_visible'):
        if status in statuses:
            return status
    return 'pass' if statuses and all(x == 'pass' for x in statuses) else 'fail'


def item_truth(truth):
    truth = dict(truth)
    if 'eye_color' in truth:
        truth['eye_color_sampled'] = truth['eye_color']
    if 'accessories' in truth:
        truth['accessory_color_sampled'] = truth['accessories']
    return truth


def summarize(samples, records, *, cohort=None):
    rows = {}
    for item in ALL_ITEMS:
        counts = {truth: Counter() for truth in ('error', 'ok', 'not_visible')}
        sample_counts = Counter()
        unstable = pairs = 0
        for sample, label in samples.items():
            if cohort and label['cohort'] != cohort:
                continue
            truth = item_truth(label['truth']).get(item)
            if truth not in counts:
                continue
            runs = [r for r in records if r['sample'] == sample]
            if len(runs) != 2:
                raise ValueError('requires two visual runs per confirmed sample')
            statuses = ([runs[0]['detection_review']['data'][item]['status']] if item in DETECTION_KEYS
                        else [r['mapped'][item] for r in runs])
            sample_counts[truth] += 1
            counts[truth].update(statuses)
            if item not in DETECTION_KEYS:
                pairs += 1
                unstable += statuses[0] != statuses[1]
        errors, oks, invisible = [sum(counts[t].values()) for t in ('error', 'ok', 'not_visible')]
        detection = counts['error']['fail'] / errors if errors else None
        false_positive = counts['ok']['fail'] / oks if oks else None
        rows[item] = {'status': 'unverified' if sample_counts['error'] < 3 or sample_counts['ok'] < 3
                      else 'reliable' if detection >= .75 and false_positive <= .20 else 'unreliable',
                      'error_samples': sample_counts['error'], 'ok_samples': sample_counts['ok'],
                      'not_visible_samples': sample_counts['not_visible'],
                      'counts': {t: dict(c) for t, c in counts.items()},
                      'fail_detection_rate': detection, 'false_positive_rate': false_positive,
                      'not_visible_fail_rate': counts['not_visible']['fail'] / invisible if invisible else None,
                      'invisible_fail': counts['not_visible']['fail'], 'invisible_total': invisible,
                      'unstable_pairs': unstable, 'total_pairs': pairs,
                      'unstable_rate': unstable / pairs if pairs else None,
                      'deterministic': item in DETECTION_KEYS}
    return rows
