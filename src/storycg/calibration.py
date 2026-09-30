"""Item-level statistics, with fixed reliability rules independent of sample contents."""
from collections import Counter

ITEMS = ['character', 'hair_color', 'hairstyle', 'eye_color', 'accessories', 'outfit', 'props', 'action']


def mapped_status(item, single, consistency):
    result = single if item in ('props', 'action') else consistency
    if result['status'] != 'pass':
        return 'fail'
    data = result['data']
    if item == 'character':
        keys = [k for k in data if k in ('hair_color', 'hairstyle', 'eye_color',
                'signature_features', 'hair_ornaments', 'accessories')]
    elif item == 'accessories':
        keys = [k for k in data if k in ('accessories', 'hair_ornaments')]
    elif item == 'outfit':
        keys = [k for k in data if k.startswith('outfit_component_') or k == 'outfit_state']
    elif item == 'props':
        keys = [k for k in data if k == 'prop_contact' or k.startswith('prop_')]
    else:
        keys = [item]
    statuses = [data.get(k, {}).get('status', 'fail') for k in keys]
    for status in ('fail', 'minor', 'not_visible'):
        if status in statuses:
            return status
    return 'pass' if statuses and all(s == 'pass' for s in statuses) else 'fail'


def summarize(labels, records):
    confirmed = {k: v for k, v in labels['samples'].items() if v['confirmed']}
    rows = {}
    for item in ITEMS:
        error_samples = [k for k, v in confirmed.items() if v['truth'].get(item) == 'error']
        ok_samples = [k for k, v in confirmed.items() if v['truth'].get(item) == 'ok']
        counts = {truth: Counter() for truth in ('error', 'ok', 'not_visible')}
        unstable = total_pairs = 0
        for sample, label in confirmed.items():
            truth = label['truth'].get(item)
            if truth not in counts:
                continue
            runs = [r for r in records if r['sample'] == sample]
            if len(runs) != 2:
                raise ValueError('requires exactly two runs per confirmed sample')
            statuses = [mapped_status(item, r['single'], r['consistency']) for r in runs]
            counts[truth].update(statuses)
            total_pairs += 1
            unstable += statuses[0] != statuses[1]
        n_error, n_ok = sum(counts['error'].values()), sum(counts['ok'].values())
        fail_detection = counts['error']['fail'] / n_error if n_error else None
        false_positive = counts['ok']['fail'] / n_ok if n_ok else None
        status = ('unverified' if len(error_samples) < 3 or len(ok_samples) < 3 else
                  'reliable' if fail_detection >= .75 and false_positive <= .20 else 'unreliable')
        rows[item] = {'status': status, 'error_samples': len(error_samples), 'ok_samples': len(ok_samples),
                      'counts': {k: dict(v) for k, v in counts.items()},
                      'fail_detection_rate': fail_detection,
                      'nonpass_detection_rate': (n_error - counts['error']['pass']) / n_error if n_error else None,
                      'false_positive_rate': false_positive,
                      'invisible_pass': counts['not_visible']['pass'],
                      'invisible_total': sum(counts['not_visible'].values()),
                      'unstable_pairs': unstable, 'total_pairs': total_pairs,
                      'unstable_rate': unstable / total_pairs if total_pairs else None}
    return rows
