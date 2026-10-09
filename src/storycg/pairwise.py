"""V2-23 (D65): side-by-side pick between the top candidates of a shot.

Used by candidates.py after the ranking. The comparison pool is the passing candidates that tie with
the top candidate on failure levels 0-3 (count/extra, identity, outfit, action/expression); prop
items never enter it. The pool is shown side by side in forward order and in reverse order, and the
letters are mapped back to candidate numbers. The pick is adopted only when both orders choose the
same candidate. Otherwise, on a call failure, a format error, a bad letter or a budget shortage, the
ranking result stays as it was, and the reason says why.

PROMPT_TEMPLATE is the V2-22 prompt, copied unchanged.
"""
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from storycg.review import TEXT, messages, object_schema

PROMPT_TEMPLATE = 'You see {n} images side by side, labelled {labels} from left to right. Each image is a candidate picture of the same character. Requirement for this one shot only:\n- framing: {framing}\n- spatial relation: {spatial_relation}\n- pose / action: {pose}\n- expression: {expression}\n- narrative role of this shot: {narrative_role}\nQuestion: which image makes the requested action and expression most readable at a glance? Judge only the visible character\'s action and expression against this requirement. For every image, say whether the requested action is visible and whether the requested expression is visible, each as yes, partly or no. Answer with JSON only, following the schema: "best" is one of the labels; "reason" is one short sentence; "per_image" has one entry per label.'
LETTERS = 'ABCDEFGH'
HEIGHT = 1024
LABEL_H = 110
GAP = 16
COMPARE_LEVELS = 4  # rank indices 0-3: count/extra, identity, outfit, action/expression
CALLS_PER_SHOT = 2


def schema_for(labels):
    per_image = {'type': 'array', 'minItems': len(labels), 'maxItems': len(labels), 'items': object_schema({
        'label': {'enum': list(labels)},
        'action_visible': {'enum': ['yes', 'partly', 'no']},
        'expression_visible': {'enum': ['yes', 'partly', 'no']}})}
    return object_schema({'best': {'enum': list(labels)}, 'reason': TEXT, 'per_image': per_image})


def shot_fields(entry):
    shot = entry['shot']
    return {k: shot.get(k, '') for k in ('framing', 'spatial_relation', 'pose', 'expression', 'narrative_role')}


def comparison_pool(records):
    """Passing candidates tied with the top one on levels 0-3, best first. Empty if none passes."""
    survivors = sorted((r for r in records if r['status'] == 'completed' and not r['screen']),
                       key=lambda r: r['rank'])
    if not survivors:
        return []
    top = survivors[0]['rank'][:COMPARE_LEVELS]
    return [r for r in survivors if r['rank'][:COMPARE_LEVELS] == top]


def composite(paths, labels, out_path):
    images = []
    for path in paths:
        with Image.open(path) as source:
            image = source.convert('RGB')
        scale = HEIGHT / image.height
        images.append(image.resize((round(image.width * scale), HEIGHT), Image.LANCZOS))
    width = sum(i.width for i in images) + GAP * (len(images) + 1)
    sheet = Image.new('RGB', (width, HEIGHT + LABEL_H + 2 * GAP), 'white')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=72)
    x = GAP
    for image, label in zip(images, labels):
        sheet.paste(image, (x, GAP))
        draw.text((x + image.width // 2 - 24, GAP + HEIGHT + 12), label, font=font, fill='black')
        x += image.width + GAP
    sheet.save(out_path, 'PNG')
    return out_path


def _classify(error):
    text = str(error or '')
    if any(word in text for word in ('Validation', 'JSON', 'incomplete', 'refused', 'not one of the labels')):
        return 'format'
    return 'call'


def _ask(vlm, root, number, entry, pool, order):
    ordered = pool if order == 'forward' else list(reversed(pool))
    labels = list(LETTERS[:len(ordered)])
    letter_to_candidate = {label: r['candidate'] for label, r in zip(labels, ordered)}
    out_dir = root / 'cycle_artifacts'
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = composite([root / r['image']['path'] for r in ordered], labels,
                      out_dir / f'pair_shot{number:02d}_{order}.png')
    text = PROMPT_TEMPLATE.format(n=len(labels), labels=', '.join(labels), **shot_fields(entry))
    schema = schema_for(labels)
    response = vlm.call(f'pairwise_shot{number}_{order}', messages(text, [sheet]), schema, [sheet])
    record = {'order': order, 'letter_to_candidate': letter_to_candidate,
              'composite': str(sheet.relative_to(root)), 'composite_sha256': hashlib.sha256(sheet.read_bytes()).hexdigest(),
              'prompt': text, 'call_id': response.get('call_id'), 'status': response.get('status'),
              'error': response.get('error'), 'data': response.get('data'),
              'elapsed_seconds': response.get('elapsed_seconds')}
    if response.get('status') == 'pass':
        best = response['data']['best']
        if best not in letter_to_candidate:
            record['status'] = 'fail'
            record['error'] = 'ValueError: best is not one of the labels'
        else:
            record['best_letter'] = best
            record['best_candidate'] = letter_to_candidate[best]
            record['reason'] = response['data']['reason']
            record['per_image'] = response['data']['per_image']
    return record


def resolve(root, number, entry, records, top, top_reason, vlm, *, budget_remaining=None):
    """Return (chosen record, reason, pairwise record) for one shot.

    `top` and `top_reason` are the ranking result. The pairwise record is stored in candidates.json.
    budget_remaining, when given, is the number of vision calls still allowed for this shot's pick.
    """
    root = Path(root)
    pool = comparison_pool(records)
    record = {'pool': [r['candidate'] for r in pool], 'decision': None, 'reason': None, 'orders': {}}
    if len(pool) < 2:
        record.update(decision='not_run', reason='比较池只有 1 张，不调用')
        return top, top_reason, record
    budget = budget_remaining
    if budget is None:
        budget = vlm.max_calls - vlm.calls
    if budget < CALLS_PER_SHOT:
        record.update(decision='reverted', reason=f'视觉调用预算不够（需要 {CALLS_PER_SHOT} 次，剩余 {budget} 次），按原排序')
        return top, f'{top_reason}；并排挑选未调用：视觉调用预算不够，按原排序', record
    results = {}
    for order in ('forward', 'reverse'):
        results[order] = _ask(vlm, root, number, entry, pool, order)
        record['orders'][order] = results[order]
        if results[order]['status'] != 'pass':
            kind = '返回格式错误' if _classify(results[order]['error']) == 'format' else '调用失败'
            record.update(decision='reverted', reason=f'并排挑选{kind}（{order}：{results[order]["error"]}），按原排序')
            return top, f'{top_reason}；并排挑选{kind}，按原排序', record
    fwd, rev = results['forward']['best_candidate'], results['reverse']['best_candidate']
    by_candidate = {r['candidate']: r for r in pool}
    if fwd == rev:
        chosen = by_candidate[fwd]
        reason = f"并排挑选，正序倒序一致：候选 {fwd}。模型理由：{results['forward']['reason']}"
        record.update(decision='adopted', reason=reason, chosen=fwd)
        return chosen, reason, record
    reason = f'并排挑选两次不一致（候选 {fwd} / 候选 {rev}），按原排序'
    record.update(decision='reverted', reason=reason)
    return top, f'{top_reason}；{reason}', record
