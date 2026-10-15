"""V2-21 (TASK-v2 §10.3): per-shot candidates, screening, selection, delivery without repair.

Default delivery. Each shot gets `candidates_per_shot` images with distinct, reproducible seeds.
Each candidate is screened by cycle.audit_candidate (V2-61, D96: local detection plus one extra-body-part
question; no per-candidate identity or single-shot review). Candidates failing the protagonist-count gate
or showing a positive extra-body-part finding are screened out. The rest go to the side-by-side pick
(pairwise.resolve); the ranking only puts a candidate with nothing detected behind its shot mates, then
orders by candidate number. A shot whose candidates all fail screening gets one more round of the same
size; if still all screened, the best of all candidates is delivered and the reason is written down.
The selected candidates' identity and outfit are reviewed once, in the final review.
Since V2-28 this is the only delivery (the repair delivery was removed).
"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from storycg import cycle as frozen
from storycg import extra_part
from storycg.budget import Budget, CycleClient, remaining_images
from storycg.config import candidates_per_shot, pairwise_enabled
from storycg.face_refine import REASONS as FACE_REASONS, refine_candidate, summary as face_refine_summary
from storycg.review import load_reliability, reliability_key
from storycg.state import load_state, save_json_atomic


def is_prop_item(key):
    return key == 'props' or key.startswith('prop_')


# V2-61 (D96): the ranking has two levels; the side-by-side pick decides among the screened-in candidates.
LEVEL_NAMES = ('nothing detected (V2-53: no face or body found, behind its shot mates)', 'candidate number')


def base_seed(config, job_id):
    """generation_seed when the job has one; otherwise a stable value derived from the job id."""
    if config.get('generation_seed') is not None:
        return int(config['generation_seed'])
    return int(hashlib.sha256(job_id.encode()).hexdigest()[:15], 16)


def candidate_seed(base, shot_number, candidate):
    """Deterministic 63-bit seed for one candidate: a function of base seed, shot and candidate number."""
    digest = hashlib.sha256(f'{base}/{shot_number}/{candidate}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)


def screen_reasons(audit):
    reasons = []
    detection = audit.get('detection_items') or {}
    item = (detection.get('review') or {}).get('data', {}).get('protagonist_count', {})
    observed = (item.get('observation') or {}).get('status', item.get('observed_status'))
    reliable = load_reliability({}).get('protagonist_count', {}).get('status') == 'reliable'
    top = (detection.get('protagonist_count') or {}).get('status')
    if reliable and (item.get('status') in ('fail', 'duplicate') or observed == 'duplicate' or top == 'duplicate'):
        reasons.append(f'人数检测不过（protagonist_count: {item.get("status") or top or observed}）')
    if extra_part.is_positive(audit):
        location = extra_part.item_of(audit).get('location', 'unknown')
        reasons.append(f'多余身体局部阳性（{location}）')
    return reasons


def nothing_detected(audit, face):
    """V2-53 (D89): the detector saw neither a face nor a body, and the refinement's search found no face either."""
    detection = (audit.get('detection_items') or {}).get('detection')
    if not detection or detection.get('faces') or detection.get('persons'):
        return False
    return (face or {}).get('status') == 'no_face'


def rank_key(audit, number, face=None):
    """V2-61 (D96): [nothing detected, candidate number]; lower is better, compared in order.

    Screened-out candidates are left out before ranking (pick), and the side-by-side pick chooses among the rest;
    the ranking decides only when that pick does not run or does not agree with itself. V2-53: a candidate with
    nothing detected (see nothing_detected) ranks behind its shot mates; it is not screened out. `face` is its
    face_refine record, if any. Before V2-61 the rank had seven levels built from the per-candidate audit.
    """
    return [1 if nothing_detected(audit, face) else 0, number]


def pick(records):
    """Choose one candidate of a shot. Returns (record or None, reason text)."""
    pool = [r for r in records if r['status'] == 'completed']
    if not pool:
        return None, '没有完成的候选'
    survivors = [r for r in pool if not r['screen']]
    use = survivors or pool
    ordered = sorted(use, key=lambda r: r['rank'])
    chosen = ordered[0]
    if not survivors:
        detail = '；'.join(f"候选{r['candidate']}：{'；'.join(r['screen'])}" for r in pool)
        return chosen, (f"这一镜所有候选都没通过筛选，按同一顺序取最好的候选{chosen['candidate']}。{detail}")
    if len(survivors) == 1:
        return chosen, '其余候选被筛掉，只剩这一张通过筛选'
    runner_up = ordered[1]
    level = next(i for i, (a, b) in enumerate(zip(chosen['rank'], runner_up['rank'])) if a != b)
    return chosen, (f"{len(survivors)} 张通过筛选的候选中，按第 {level + 1} 级（{LEVEL_NAMES[level]}）分出胜负："
                    f"候选{chosen['candidate']} 的 {chosen['rank'][level]} 优于候选{runner_up['candidate']} 的 {runner_up['rank'][level]}")


def _store_path(root):
    return root / 'candidates.json'


def _load_store(root):
    path = _store_path(root)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def _save_store(root, store):
    save_json_atomic(_store_path(root), store)


def _reserve(state, board, number, numbers, per):
    """V2-52: images to keep for the candidates still to come: this shot's `numbers` (the round in progress)
    without an attempt, and the first round of the shots after this one without an attempt."""
    made = {(a['shot_id'], a['candidate']) for a in state['attempts'] if a.get('candidate') is not None}
    shots = [e['number'] for e in board['shots']]
    own = sum(1 for n in numbers if (f'shot{number:02d}', n) not in made)
    later = shots[shots.index(number) + 1:]
    return own + sum(1 for m in later for c in range(1, per + 1) if (f'shot{m:02d}', c) not in made)


def _run_round(root, store, entry, board, identity, config, client, vlm, budget, base, parts, shot_config, numbers, round_no, face_ref):
    from storycg.cycle import audit_candidate
    from storycg.runner import run_candidate
    slot = store[str(entry['number'])]
    number = entry['number']
    shot_id = f'shot{number:02d}'
    for n in numbers:
        if any(r['candidate'] == n for r in slot['candidates']):
            continue  # resume: this candidate was already generated and audited
        budget.check()
        seed = candidate_seed(base, number, n)
        run_candidate(root, shot_id, n, seed, parts['positive_tags'], parts['framing'], shot_config, client,
                      negative_prompt=shot_config['negative_prompt'], generation_reference=face_ref,
                      dropped_scene_tags=parts['dropped_scene_tags'],
                      indoor_negative=parts['indoor_precipitation_negative'])
        state = load_state(root)
        attempt = [a for a in state['attempts'] if a['shot_id'] == shot_id and a.get('candidate') == n][-1]
        face = None
        if attempt['status'] == 'completed':
            # V2-50 (D87): a small face is refined before the audit; the audit and delivery then see the composite.
            reserve = _reserve(state, board, number, numbers, candidates_per_shot(config))
            face = refine_candidate(root, shot_id, n, parts=parts, client=client, budget=budget, reserve=reserve)
            state = load_state(root)
            attempt = [a for a in state['attempts'] if a['shot_id'] == shot_id and a.get('candidate') == n][-1]
        label = f'cand_{number:02d}_{n:02d}'
        record = {'candidate': n, 'round': round_no, 'seed': seed, 'attempt_number': attempt['attempt_number'],
                  'status': attempt['status'], 'image': None, 'screen': [], 'rank': None,
                  'audit_label': label, 'checked': False, 'prompt_id': attempt.get('prompt_id')}
        if face is not None:
            record['face_refine'] = face_refine_summary(face)
        if attempt['status'] == 'completed':
            record['image'] = {'path': attempt['image']['path'], 'sha256': attempt['image']['sha256']}
            audit = audit_candidate(root, number, attempt['image'], entry, board, identity, config, vlm, label)
            record['screen'] = screen_reasons(audit)
            record['rank'] = rank_key(audit, n, attempt.get('face_refine'))
            record['checked'] = True  # V2-61 (Q4 of D96): no conclusion is stored, only the screen and the rank
            if nothing_detected(audit, attempt.get('face_refine')):
                record['nothing_detected'] = True
        else:
            record['screen'] = [f"生成未完成（{attempt['status']}）"]
        slot['candidates'].append(record)
        _save_store(root, store)


def run_candidate_delivery(job_dir, config, client, identity, board, face_ref, *, budget=None):
    """Generate, screen and select candidates for every shot, then deliver. Returns the job state."""
    from storycg.assembly import assemble_prompt, full_negative_prompt
    from storycg.cycle import settings
    root = Path(job_dir).resolve()
    cycle_cfg = settings()
    budget = budget or Budget(root, cycle_cfg)
    per = candidates_per_shot(config)
    base = base_seed(config, root.name)
    store = _load_store(root)
    reserve = 3 * len(board['shots']) + 4  # full review after selection
    vlm = CycleClient(root, config['model_service_url'], budget=budget,
                      max_calls=max(0, cycle_cfg['max_vision_calls'] - budget.usage()['review_calls'] - reserve),
                      max_seconds=None)
    try:
        for entry in board['shots']:
            number = entry['number']
            key = str(number)
            slot = store.setdefault(key, {'candidates': [], 'selected': None, 'reason': None, 'rounds': 1})
            parts = assemble_prompt(identity, entry, config=config,
                                    **({'props': board['props']} if board.get('props') else {}))
            shot_config = copy.deepcopy(config)
            shot_config['negative_prompt'] = full_negative_prompt(config, parts)
            args = (root, store, entry, board, identity, config, client, vlm, budget, base, parts, shot_config)
            _run_round(*args, range(1, per + 1), 1, face_ref)
            if not any(r['status'] == 'completed' and not r['screen'] for r in slot['candidates']):
                # V2-52: the second round runs when it has started (its candidates are in state.json, even if
                # candidates.json was not written yet) or when the budget covers it and the later shots' first rounds.
                state = load_state(root)
                second = range(per + 1, 2 * per + 1)
                started = slot['rounds'] == 2 or any(a['shot_id'] == f'shot{number:02d}' and a.get('candidate') in second
                                                     for a in state['attempts'])
                needed = _reserve(state, board, number, second, per)
                remaining = remaining_images(budget, state)
                if started or remaining >= needed:
                    slot['rounds'] = 2
                    slot.pop('second_round_skipped', None)
                    _run_round(*args, range(per + 1, 2 * per + 1), 2, face_ref)
                else:
                    slot['second_round_skipped'] = {'reason': 'budget', 'remaining': remaining, 'needed': needed}
            chosen, reason = pick(slot['candidates'])
            if chosen is not None and pairwise_enabled(config):
                # V2-23 (D65): the side-by-side pick may replace the ranking result; it never fails the shot.
                from storycg.pairwise import resolve
                chosen, reason, slot['pairwise'] = resolve(root, number, entry, slot['candidates'], chosen, reason, vlm)
            slot['selected'] = chosen['candidate'] if chosen else None
            slot['reason'] = reason
            _save_store(root, store)
    finally:
        vlm.close()
    return _finish(root, config, client, identity, board, store, cycle_cfg)


def _finish(root, config, client, identity, board, store, cycle_cfg):
    from storycg.delivery import deliver
    from storycg.shot_vocab import record_observations
    cycle = {'version': 1, 'config': cycle_cfg, 'shots': {}, 'phase': 'candidate_selection',
             'allow_obscured': config.get('allow_obscured_protagonist', False), 'delivery_mode': 'candidates'}
    for entry in board['shots']:
        number = entry['number']
        slot = store[str(number)]
        if slot['selected'] is None:
            raise ValueError(f'shot {number} has no completed candidate')
        record = next(r for r in slot['candidates'] if r['candidate'] == slot['selected'])
        audit = json.loads((root / 'cycle_artifacts' / f"{record['audit_label']}.json").read_text(encoding='utf-8'))
        cycle['shots'][str(number)] = {'best_attempt': record['attempt_number'], 'best_audit': audit, 'entry': entry,
                                       'history': [], 'stagnant': 0, 'stop': 'candidate_selected'}
    write_candidate_sheet(root, store)
    deliver(root, cycle, config, client)
    annotate(root, store)
    fingerprint = hashlib.sha256(json.dumps([(n, s['selected']) for n, s in sorted(store.items())],
                                            sort_keys=True).encode()).hexdigest()[:16]
    final_path = root / 'cycle_artifacts' / f'final_review_candidates_{fingerprint}.json'
    if final_path.exists():
        report = json.loads(final_path.read_text(encoding='utf-8'))
    else:
        report = frozen.full_review(root, config, Budget(root, cycle_cfg))
        save_json_atomic(final_path, report)
    cycle['final_review'] = report
    cycle['phase'] = 'complete'
    cycle['finished_at'] = datetime.now(timezone.utc).isoformat()
    deliver(root, cycle, config, client)
    annotate(root, store)
    save_json_atomic(root / 'cycle.json', cycle)
    record_observations(root, config)
    return load_state(root)


THUMB_LONG_SIDE = 512
SCREEN_CODES = (('人数检测', 'count'), ('多余身体局部', 'extra_part'), ('生成未完成', 'generation'))


def screen_code(reason):
    for prefix, code in SCREEN_CODES:
        if reason.startswith(prefix):
            return code
    return 'other'


def _read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding='utf-8'))


def _passed(slot):
    return any(r['status'] == 'completed' and not r['screen'] for r in slot['candidates'])


DEVIATION_LINES_PER_SHOT = 6
BLIND_ITEMS = ('where', 'event', 'action', 'change', 'ending', 'uncertainty')


def _audit_path(root, selected):
    return root / 'cycle_artifacts' / f"{selected['audit_label']}.json"


def _final_consistency(cycle, number):
    """V2-61: the final review's identity/outfit/scene items of one shot (the only identity review since V2-61)."""
    for entry in (cycle.get('final_review') or {}).get('consistency') or []:
        if entry.get('shot') == number:
            return entry.get('data') or {}
    return {}


def _unreliable_keys(root, selected, cycle=None, number=None):
    """Categories not reliably reviewed for this shot: the selected candidate's own audit (jobs before V2-61)
    and the final review's consistency items of the shot (read only)."""
    keys = []
    if selected is not None and _audit_path(root, selected).exists():
        from storycg.display import read_json
        audit = read_json(_audit_path(root, selected))
        for part in ('single', 'consistency'):
            for key, item in ((audit.get(part) or {}).get('data') or {}).items():
                if isinstance(item, dict) and not is_prop_item(key) and item.get('reliability') != 'reliable':
                    keys.append(key)
    for key, item in (_final_consistency(cycle or {}, number).items() if number is not None else []):
        # Scene items are the final review's own scene check, not an identity category.
        if isinstance(item, dict) and not is_prop_item(key) and not key.startswith('scene_') \
                and item.get('reliability') != 'reliable':
            keys.append(key)
    return list(dict.fromkeys(keys))


def _audit_deviation_items(root, selected):
    """V2-32 / V2-45: the selected candidate's fail or minor items from its own audit, uncapped (read only)."""
    if selected is None or not _audit_path(root, selected).exists():
        return []
    from storycg.display import read_json, flat
    audit = read_json(_audit_path(root, selected))
    deviations = []
    for part, label in (('single', '单镜'), ('consistency', '一致性')):
        for key, item in ((audit.get(part) or {}).get('data') or {}).items():
            if not isinstance(item, dict) or is_prop_item(key):
                continue
            if item.get('status') in ('fail', 'minor'):
                tag = '可靠' if item.get('reliability') == 'reliable' else '未经可靠审核'
                deviations.append(f"- 审核观察到的偏差：{label} {key} {item['status']}（{tag}）：{flat(item.get('reason'))}")
    return deviations


def _audit_deviation_lines(root, selected):
    """V2-32: the selected candidate's own audit, shaped into result.md lines. Only fail/minor items are reported
    as deviations; the verdicts are the stored ones, not recomputed here."""
    if selected is None:
        return ['- 没有选中候选，无审核记录。']
    from storycg.display import flat
    head = ('- 通过了筛选：人数检查、多余肢体检查' if not selected['screen']
            else '- 通过了筛选：否（' + '；'.join(selected['screen']) + '，用户指定保留）')
    audit_path = _audit_path(root, selected)
    if not audit_path.exists():
        return [head, f"- 审核记录缺失：cycle_artifacts/{selected['audit_label']}.json"]
    deviations = _audit_deviation_items(root, selected)
    lines = [head]
    from storycg.display import read_json
    reviewed_here = any(key in read_json(audit_path) for key in ('single', 'consistency'))
    if not deviations and reviewed_here:  # V2-61: a screen record has no review of its own; the final review follows
        lines.append('- 审核观察到的偏差：无（没有 fail 或 minor 项）')
    # Six lines per shot at most: the head always stays; deviations take the rest.
    room = DEVIATION_LINES_PER_SHOT - 1
    if len(deviations) > room:
        deviations = deviations[:room - 1] + [f'- 另有 {len(deviations) - (room - 1)} 项，见 review.md']
    lines += deviations
    return lines


def _shot_is_clean(root, cycle, number, selected):
    """V2-45: nothing to list for this shot: the selected candidate passed the screen, its own audit has no fail
    or minor item, and the final review's consistency has nothing to list for it."""
    return (selected is not None and not selected['screen'] and _audit_path(root, selected).exists()
            and not _audit_deviation_items(root, selected)
            and not _final_shot_lines(root, cycle, number, selected))


def _portrait_item_text(item):
    """V2-33: one failed portrait item; detection items are marked, and so is whether the item is a reliable one
    (result.md carries no per-item "not reliably reviewed" sentence; that stays in review.md)."""
    source = '，检测项' if item.get('source') == 'detection' else ''
    kind = '可靠项' if item.get('reliable', True) else '非可靠项'
    return f"{item['item']}（{item['status']}{source}）：{item['reason'].rstrip('.。')}（{kind}）"


def _group_deviation_lines(root, group, skipped=False):
    """V2-32 / V2-45: the group's unreliable categories, plus the raw portrait review's reliable failed items.
    No layer conclusions. The portrait line is written only when a reliable item did not pass."""
    from storycg.display import portrait_review
    names = list(dict.fromkeys(group))
    lines = [f"- 至少一镜未经可靠审核的类别：{'、'.join(names)}" if names else '- 至少一镜未经可靠审核的类别：无']
    if skipped:  # P0.2: "无" would read as reviewed; the per-shot identity/outfit review did not run
        lines = ['- 未进行逐镜身份服装终审（默认关闭）：身份、服装、配饰请看图确认；下面写“通过筛选”只表示人数和多余肢体检查通过。']
    portrait = portrait_review(root) or {}
    failed = [x for x in portrait.get('failed_items') or [] if x.get('reliable', True)]
    if failed:
        items = '；'.join(_portrait_item_text(x) for x in failed)
        lines.append(f"- 定妆图原始审核未通过：{items}。{portrait['note']}。")
    return lines



def _candidate_deviation_keys(root, selected):
    """V2-34: keys the selected candidate's own audit already reports as fail or minor (read only)."""
    if selected is None or not _audit_path(root, selected).exists():
        return set()
    from storycg.display import read_json
    audit = read_json(_audit_path(root, selected))
    return {key for part in ('single', 'consistency')
            for key, item in ((audit.get(part) or {}).get('data') or {}).items()
            if isinstance(item, dict) and item.get('status') in ('fail', 'minor') and not is_prop_item(key)}


def _item_reason(item):
    """The reason of a review item; the extra-body-part item keeps its reason one level down."""
    if item.get('reason'):
        return item['reason']
    for value in (item.get('data') or {}).values():
        if isinstance(value, dict) and value.get('reason'):
            return value['reason']
    return ''


def _final_narrative_lines(cycle):
    """V2-34: the narrative comparison of the final review, every item that is not pass, with its full reason."""
    narrative = ((cycle.get('final_review') or {}).get('narrative_compare') or {}).get('data') or {}
    lines = []
    for key, item in narrative.items():
        if isinstance(item, dict) and item.get('status') != 'pass':
            reason = ' '.join(str(_item_reason(item)).split()) or '未记录原因'
            lines.append(f"- 终审 · 剧情对照 · {key}（{item.get('status')}）：{reason}")
    return lines or ['- 终审 · 剧情对照：没有非 pass 的项。']


def _final_shot_lines(root, cycle, number, selected):
    """V2-34: the final review's consistency items of one shot that are fail or minor and that the
    selected candidate's own audit did not report. At most DEVIATION_LINES_PER_SHOT lines per shot.
    V2-42: only the consistency layer is read; the final review no longer re-runs the single-shot review."""
    from storycg.display import flat
    final = cycle.get('final_review') or {}
    known = _candidate_deviation_keys(root, selected)
    deviations = []
    for entry in final.get('consistency') or []:
        if entry.get('shot') != number:
            continue
        for key, item in (entry.get('data') or {}).items():
            if not isinstance(item, dict) or is_prop_item(key) or key in known:
                continue
            if item.get('status') in ('fail', 'minor'):
                tag = '可靠' if item.get('reliability') == 'reliable' else '未经可靠审核'
                deviations.append(f"- 终审 · 一致性 · {key}（{item['status']}，{tag}）：{flat(_item_reason(item))}")
    if not deviations:
        return []
    if len(deviations) > DEVIATION_LINES_PER_SHOT:
        deviations = deviations[:DEVIATION_LINES_PER_SHOT - 1] + [
            f'- 终审另有 {len(deviations) - (DEVIATION_LINES_PER_SHOT - 1)} 项，见 review.md']
    return deviations


def vision_failures(root):
    """V2-65: (failed calls, all calls) of the vision service in this job, from review_log.jsonl (read only)."""
    path = Path(root) / 'review_log.jsonl'
    if not path.exists():
        return 0, 0
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = [r for r in rows if r.get('event') == 'response']
    return sum(1 for r in responses if r.get('status') != 'pass'), len(responses)


def result_text(root, store=None):
    """V2-24 (TASK-v2 §10.3 / §10.4 notes): the whole result.md of a candidate job, in the order
    overview, files, each shot, blind narrative, known issues, detail pointer. No group verdict and no
    per-item 'not reliably reviewed' lines; those stay in review.md."""
    root = Path(root)
    store = _load_store(root) if store is None else store
    job = _read_json(root / 'job.json')
    identity = _read_json(root / 'identity.json')
    board = _read_json(root / 'storyboard.json')
    cycle = _read_json(root / 'cycle.json')
    manifest = _read_json(root / 'manifest.json')
    agent = _read_json(root / 'agent.json')
    portrait_attempts = _read_json(root / 'portrait_attempts.json', [])
    usage = manifest.get('usage', {})
    numbers = [e['number'] for e in board.get('shots', [])] or sorted(int(k) for k in store)
    shot_images = sum(1 for slot in store.values() for r in slot['candidates'] if r['status'] == 'completed')
    refined = sum(1 for slot in store.values() for r in slot['candidates']
                  if (r.get('face_refine') or {}).get('status') == 'refined')
    seconds = usage.get('wall_elapsed_seconds', usage.get('elapsed_seconds'))
    delivery_files = sorted(p.name for p in (root / 'delivery').glob('*.jpg') if p.name != 'candidate_sheet.jpg') \
        if (root / 'delivery').is_dir() else []
    lines = ['# 结果说明', '', '## 概况', '',
             f"- 角色：{job.get('character', '')}（{identity.get('character_tag', '')}）",
             f"- 剧情：{job.get('story', '')}",
             f"- 镜数：{len(numbers)}",
             (f"- 本组出图：{shot_images + refined + len(portrait_attempts)} 张（镜头候选 {shot_images} 张，面部精修 {refined} 张，定妆图 {len(portrait_attempts)} 张）"
              if refined else
              f"- 本组出图：{shot_images + len(portrait_attempts)} 张（镜头候选 {shot_images} 张，定妆图 {len(portrait_attempts)} 张）"),
             f"- 用时：{round(seconds / 60, 1)} 分钟" if seconds is not None else '- 用时：未记录',
             '', '## 文件', '']
    lines.append('- 拼图：' + (f"delivery/{delivery_files[0]}" if delivery_files else 'contact_sheet.jpg'))
    lines.append('- 候选总图：candidate_sheet.jpg（delivery/candidate_sheet.jpg 同图）')
    lines += ['', '## 每镜', '']
    for number in numbers:
        slot = store.get(str(number))
        if slot is None:
            lines.append(f'### 第 {number} 镜')
            lines.append('- 没有候选记录。')
            lines.append('')
            continue
        lines.append(f'### 第 {number} 镜')
        selected = next((r for r in slot['candidates'] if r['candidate'] == slot['selected']), None)
        lines.append(f"- 选中：候选 {slot['selected']}（第 {slot['rounds']} 轮生成）" if selected else '- 选中：无')
        lines.append(f"- 挑选理由：{slot['reason']}")
        face = (selected or {}).get('face_refine') or {}
        if face.get('status') == 'refined':
            lines.append(f"- 面部精修：脸长边 {round(face['face_long_side'])} 像素，已精修；原图 {face['original_image']}")
        elif face.get('status') in FACE_REASONS:
            lines.append(f"- 面部精修：脸长边 {round(face['face_long_side'])} 像素，没有精修（{FACE_REASONS[face['status']]}）")
        if selected is not None and selected['screen'] and _passed(slot):
            lines.append(f"- 提示：选中的候选 {slot['selected']} 没有通过筛选（{'；'.join(selected['screen'])}），是用户指定的。")
        screened = [r for r in slot['candidates'] if r['status'] == 'completed' and r['screen']]
        failed = [r for r in slot['candidates'] if r['status'] != 'completed']
        for r in screened:
            lines.append(f"- 被筛掉：候选 {r['candidate']}（{'；'.join(r['screen'])}）")
        for r in failed:
            lines.append(f"- 未完成：候选 {r['candidate']}（{'；'.join(r['screen'])}）")
        if not _passed(slot):
            lines.append('- 所有候选都没通过筛选：交付的是按排序取到的最好候选，请人工复看。')
            if slot.get('second_round_skipped'):
                skipped = slot['second_round_skipped']
                lines.append(f"- 第二轮没有补：出图额度不够（剩 {skipped['remaining']} 张，需要 {skipped['needed']} 张）")
        auto = slot.get('auto_selected', slot['selected'])
        if slot.get('user_pick') is not None and slot['user_pick'] != auto:
            lines.append(f"- 由用户换过：第 {number} 镜由用户换成候选 {slot['selected']}；看图复述是换之前那组图的。"
                         f"（审核器原选候选 {auto}）")
        lines.append('')
    picks = {}
    for number in numbers:
        slot = store.get(str(number))
        picks[number] = None if slot is None else next((r for r in slot['candidates'] if r['candidate'] == slot['selected']), None)
    group = list(dict.fromkeys(k for number, selected in picks.items()
                               for k in _unreliable_keys(root, selected, cycle, number)))
    lines += ['## 已观察到的偏差', '']
    failed_calls, all_calls = vision_failures(root)
    if failed_calls:  # V2-65: a service that was down must not read as "no deviations"
        lines.append(f'- **视觉审核有 {failed_calls}/{all_calls} 次调用失败**（服务不可用或超时）：失败的部分没有审核结果，'
                     '下面写“没有观察到偏差”的地方只表示没有记录，不表示审核通过。请看图确认。')
    skipped = (cycle.get('final_review') or {}).get('per_shot_consistency') == 'skipped'
    lines += _group_deviation_lines(root, group, skipped)
    lines.append('')
    # V2-34: the final review's narrative comparison is reported here; its per-shot items follow each shot.
    lines += ['### 终审发现', '']
    lines += _final_narrative_lines(cycle)
    lines.append('')
    # V2-45: a shot with nothing to list gets no subsection; the clean shots are summed up in one line at the end.
    clean = []
    for number in numbers:
        if not failed_calls and _shot_is_clean(root, cycle, number, picks[number]):
            clean.append(number)
            continue
        lines.append(f'### 第 {number} 镜')
        lines += _audit_deviation_lines(root, picks[number])
        lines += _final_shot_lines(root, cycle, number, picks[number])
        lines.append('')
    if clean:
        lines.append(f"- 第 {'、'.join(str(n) for n in clean)} 镜：通过筛选" + ('（未做逐镜身份服装终审）。' if skipped else '，没有观察到偏差。'))
        lines.append('')
    from storycg.display import flat
    # V2-36: all six blind items in this order; an item that is missing or empty is skipped.
    blind = (cycle.get('final_review') or {}).get('narrative_blind', {}).get('data', {})
    blind_lines = [f"- {key}：{flat(blind[key])}" for key in BLIND_ITEMS if blind.get(key)]
    lines += ['## 看图复述', '', '以下是终审的盲审模型只看图写的，不是剧情原文：', '']
    lines += blind_lines or ['- 终审没有给出看图复述。']
    lines.append('')
    issues = []
    if (board.get('props') or any(e['shot'].get('key_props') or e['shot'].get('prop_tags') for e in board.get('shots', []))):
        issues.append('道具未经审核，只作陪衬，数量、归属、状态不保证。')
    for number in numbers:
        slot = store.get(str(number))
        if slot is not None and not _passed(slot):
            if slot.get('second_round_skipped'):
                issues.append(f'第 {number} 镜所有候选都没通过筛选，因出图额度不够没有补第二轮，交付的是第一轮里排序最好的候选，请人工复看。')
            else:
                issues.append(f'第 {number} 镜所有候选都没通过筛选，交付的是按排序取到的最好候选，请人工复看。')
    for number in numbers:
        slot = store.get(str(number))
        selected = next((r for r in slot['candidates'] if r['candidate'] == slot['selected']), None) if slot else None
        if selected is not None and selected.get('nothing_detected'):
            issues.append(f'第 {number} 镜选中的候选里，本机检测器没找到人，放大检测也没找到脸，主角可能很小或没画出来，请看图确认。')
    if failed_calls:
        issues.append(f'视觉审核有 {failed_calls}/{all_calls} 次调用失败：多余肢体检查、并排挑选、整组终审里失败的部分没有结果，'
                      '挑选可能退回按候选号，看图复述可能缺失，请人工看图。')
    issues.append('多余肢体检查只筛掉明确阳性的候选，可能漏检，请看图复核手和腿。')
    if board.get('story_note'):
        issues.append('分镜改写提醒（story_note）：' + board['story_note'])
    if 'Budget' in str(agent.get('error') or '') or 'budget' in str(agent.get('error') or ''):
        issues.append(f"出图或审核调用额度用完，任务提前结束：{agent.get('error')}")
    lines += ['## 已知问题', '']
    lines += [f'- {x}' for x in issues]
    lines += ['', '## 详细审核', '', '见 review.md 和 manifest.json。', '']
    return '\n'.join(lines)


def write_result_md(root, store=None):
    """Rewrite result.md of a candidate job; a no-op for jobs without candidates.json (old repair jobs)."""
    root = Path(root)
    if not _store_path(root).exists():
        return
    (root / 'result.md').write_text(result_text(root, store), encoding='utf-8')


def publish_candidates(root):
    """Candidate artefacts for every delivery write: sheet at the job root and in delivery/, result.md,
    manifest records. A no-op for jobs without a candidate store (old repair jobs)."""
    root = Path(root)
    if not _store_path(root).exists():
        return None
    store = _load_store(root)
    sheet = write_candidate_sheet(root, store)
    if sheet is not None:
        delivery = root / 'delivery'
        delivery.mkdir(parents=True, exist_ok=True)
        (delivery / 'candidate_sheet.jpg').write_bytes(sheet.read_bytes())
    write_result_md(root, store)
    manifest_path = root / 'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['candidates'] = candidate_records(store)
        manifest['candidate_sheet'] = 'candidate_sheet.jpg'
        save_json_atomic(manifest_path, manifest)
    return sheet


def pick_candidate(root, number, candidate):
    """V2-24: the user swaps the selected candidate of one shot. Only candidates.json changes here; the caller
    rebuilds the delivery. The first swap remembers the reviewer's choice so repeated calls stay identical."""
    root = Path(root)
    store = _load_store(root)
    slot = store.get(str(number))
    if slot is None:
        raise ValueError(f'shot {number} has no candidates')
    record = next((r for r in slot['candidates'] if r['candidate'] == candidate), None)
    if record is None:
        raise ValueError(f'shot {number} has no candidate {candidate}')
    if record['status'] != 'completed' or not record.get('image'):
        raise ValueError(f'candidate {candidate} of shot {number} is not a completed image')
    if 'auto_selected' not in slot:
        slot['auto_selected'] = slot['selected']
        slot['auto_reason'] = slot['reason']
    auto = slot['auto_selected']
    slot['selected'] = candidate
    slot['user_pick'] = candidate
    slot['reason'] = (slot['auto_reason'] if candidate == auto
                      else f'由用户换成候选{candidate}（审核器原选候选{auto}）')
    _save_store(root, store)
    return {'shot': int(number), 'candidate': candidate, 'auto_selected': auto,
            'screened': bool(record['screen']), 'audit_label': record['audit_label'],
            'attempt_number': record['attempt_number']}


def candidate_records(store):
    records = []
    for key, slot in sorted(store.items(), key=lambda kv: int(kv[0])):
        number = int(key)
        for r in slot['candidates']:
            record = {'shot': number, 'candidate': r['candidate'], 'round': r['round'], 'seed': r['seed'],
                      'attempt_number': r['attempt_number'], 'status': r['status'],
                      'image': r['image'], 'screen': r['screen'],
                      'screen_codes': [screen_code(x) for x in r['screen']], 'rank': r['rank'],
                      'selected': r['candidate'] == slot['selected'],
                      'reason': slot['reason'] if r['candidate'] == slot['selected'] else None,
                      'user_pick': r['candidate'] == slot['selected'] and slot.get('user_pick') is not None,
                      'prompt_id': r.get('prompt_id'), 'face_refine': r.get('face_refine')}
            if r.get('nothing_detected'):
                record['nothing_detected'] = True
            records.append(record)
    return records


def annotate(root, store):
    """Manifest records and the result.md section (the section is re-applied by publish_candidates)."""
    manifest_path = root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['candidates'] = candidate_records(store)
    manifest['candidate_sheet'] = 'candidate_sheet.jpg'
    save_json_atomic(manifest_path, manifest)
    write_result_md(root, store)


def write_candidate_sheet(root, store):
    """One row per shot, every completed candidate left to right. Thumbnails have a 512 px long side;
    labels are ASCII: shot, candidate, SELECTED / kept / SCREENED, and a reason code when screened."""
    from PIL import ImageFont
    rows = []
    for key, slot in sorted(store.items(), key=lambda kv: int(kv[0])):
        done = sorted((r for r in slot['candidates'] if r['status'] == 'completed'), key=lambda r: r['candidate'])
        rows.append((int(key), done, slot))
    if not rows:
        return None
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:  # Pillow without sized default fonts
        font = ImageFont.load_default()
    label_h, margin = 40, 16
    cells = []
    for number, done, slot in rows:
        row = []
        for r in done:
            with Image.open(root / r['image']['path']) as source:
                image = source.convert('RGB')
            scale = THUMB_LONG_SIDE / max(image.size)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            row.append((r, image.resize(size, Image.LANCZOS)))
        cells.append((number, row, slot))
    row_heights = [max([t.height for _, t in row] or [0]) + label_h + margin for _, row, _ in cells]
    row_widths = [sum(t.width for _, t in row) + margin * (len(row) + 1) for _, row, _ in cells]
    width = max(row_widths) + margin
    height = sum(row_heights) + margin
    sheet = Image.new('RGB', (width, height), '#e8e8e8')
    draw = ImageDraw.Draw(sheet)
    y = margin
    for (number, row, slot), row_h in zip(cells, row_heights):
        x = margin
        for r, thumb in row:
            sheet.paste(thumb, (x, y))
            if r['candidate'] == slot['selected']:
                state = 'SELECTED'
            elif r['screen']:
                state = 'SCREENED ' + ','.join(screen_code(s) for s in r['screen'])
            else:
                state = 'kept'
            draw.text((x, y + thumb.height + 6), f"shot{number:02d} cand{r['candidate']} {state}", font=font, fill='black')
            x += thumb.width + margin
        y += row_h
    path = root / 'candidate_sheet.jpg'
    sheet.save(path, 'JPEG', quality=90)
    return path
