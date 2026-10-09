"""V2-21 (TASK-v2 §10.3): per-shot candidates, screening, selection, delivery without repair.

Default delivery. Each shot gets `candidates_per_shot` images with distinct, reproducible seeds.
Each candidate is audited by the existing audit_candidate (no new kind of model call). Candidates
failing the protagonist-count gate or showing a positive extra-body-part finding are screened out.
The selection order is the failure tiers of TASK-v2 §0 (identity, then action/expression, then
minor count, then candidate number). A shot whose candidates all fail screening gets one more
round of the same size; if still all screened, the best of all candidates is delivered and the
reason is written down. There is no repair, no narrative repair, and no Qwen workflow here.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from storycg import cycle as frozen
from storycg import extra_part
from storycg.budget import Budget, CycleClient
from storycg.config import candidates_per_shot, pairwise_enabled
from storycg.review import load_reliability, reliability_key
from storycg.v1_scope import is_prop_item
from storycg.state import load_state, save_json_atomic

LEVEL_NAMES = ('count and extra body part (D29 level 0)', 'identity item (D29 level 1)',
               'outfit (D29 level 2)', 'action and expression (reliable items)',
               'other reliable items (not props)', 'minor count (not props)', 'candidate number')
# Prop items (TASK-v2 §0: props are décor, never a veto) take no part in the comparison at all.
ACTION_EXPRESSION_KEYS = ('action', 'expression')
RANK_CRITICAL = 99  # an audit with no data ranks last; it is never preferred over a complete audit


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
        location = audit['consistency']['data'][extra_part.ITEM].get('location', 'unknown')
        reasons.append(f'多余身体局部阳性（{location}）')
    return reasons


def rank_key(audit, number):
    """Lower is better, compared lexicographically over LEVEL_NAMES (V2-21 rework 1).

    Levels 0-2 are the failure tiers of cycle.failure_tier (count/extra, identity, outfit); level 3
    is action and expression; level 4 is every other reliable failure; level 5 is the minor count;
    level 6 is the candidate number. Prop items (props, prop_*) are left out of every level,
    including minor. Only reliable failures count; cycle.failure_tier is used unchanged.
    """
    results = [audit.get('single', {}), audit.get('consistency', {})]
    if not all(r.get('data') for r in results):
        return [RANK_CRITICAL, 0, 0, 0, 0, 0, number]
    levels = [0, 0, 0, 0, 0]
    minor = 0
    for result in results:
        for key, item in result.get('data', {}).items():
            if not isinstance(item, dict) or is_prop_item(key):
                continue
            if item.get('status') == 'minor':
                minor += 1
            if item.get('status') == 'fail' and item.get('reliability') == 'reliable':
                tier = frozen.failure_tier(key)
                if tier < 3:
                    levels[tier] += 1
                elif reliability_key(key) in ACTION_EXPRESSION_KEYS:
                    levels[3] += 1
                else:
                    levels[4] += 1
    return [levels[0], levels[1], levels[2], levels[3], levels[4], minor, number]


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
        label = f'cand_{number:02d}_{n:02d}'
        record = {'candidate': n, 'round': round_no, 'seed': seed, 'attempt_number': attempt['attempt_number'],
                  'status': attempt['status'], 'image': None, 'screen': [], 'rank': None,
                  'audit_label': label, 'conclusion': None, 'prompt_id': attempt.get('prompt_id')}
        if attempt['status'] == 'completed':
            record['image'] = {'path': attempt['image']['path'], 'sha256': attempt['image']['sha256']}
            audit = audit_candidate(root, number, attempt['image'], entry, board, identity, config, vlm, label)
            record['screen'] = screen_reasons(audit)
            record['rank'] = rank_key(audit, n)
            record['conclusion'] = audit['conclusion']
        else:
            record['screen'] = [f"生成未完成（{attempt['status']}）"]
        slot['candidates'].append(record)
        _save_store(root, store)


def run_candidate_delivery(job_dir, config, client, identity, board, face_ref, *, budget=None):
    """Generate, screen and select candidates for every shot, then deliver. Returns the job state."""
    from storycg.assembly import assemble_prompt, full_negative_prompt
    from storycg.cycle import settings
    root = Path(job_dir).resolve()
    if config.get('layout_mode') == 'qwen':
        raise ValueError('layout_mode qwen belongs to the repair delivery; set delivery_mode to repair')
    cycle_cfg = settings()
    budget = budget or Budget(root, cycle_cfg)
    per = candidates_per_shot(config)
    base = base_seed(config, root.name)
    store = _load_store(root)
    reserve = 3 * len(board['shots']) + 4  # full review after selection
    vlm = CycleClient(root, config['model_service_url'], budget=budget,
                      max_calls=max(0, cycle_cfg['max_vision_calls'] - budget.usage()['review_calls'] - reserve),
                      max_seconds=budget.remaining_seconds())
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
                slot['rounds'] = 2
                _run_round(*args, range(per + 1, 2 * per + 1), 2, face_ref)
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
    from storycg.runtime_gate import deliver
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
    seconds = usage.get('wall_elapsed_seconds', usage.get('elapsed_seconds'))
    delivery_files = sorted(p.name for p in (root / 'delivery').glob('*.jpg') if p.name != 'candidate_sheet.jpg') \
        if (root / 'delivery').is_dir() else []
    lines = ['# 结果说明', '', '## 概况', '',
             f"- 角色：{job.get('character', '')}（{identity.get('character_tag', '')}）",
             f"- 剧情：{job.get('story', '')}",
             f"- 层级：{identity.get('identity_tier', '未知')}",
             f"- 镜数：{len(numbers)}",
             f"- 本组出图：{shot_images + len(portrait_attempts)} 张（镜头候选 {shot_images} 张，定妆图 {len(portrait_attempts)} 张）",
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
        auto = slot.get('auto_selected', slot['selected'])
        if slot.get('user_pick') is not None and slot['user_pick'] != auto:
            lines.append(f"- 由用户换过：第 {number} 镜由用户换成候选 {slot['selected']}；看图复述是换之前那组图的。"
                         f"（审核器原选候选 {auto}）")
        else:
            lines.append('- 由用户换过：否')
        lines.append('')
    blind = (cycle.get('final_review') or {}).get('narrative_blind', {}).get('data', {})
    lines += ['## 看图复述', '', '以下是终审的盲审模型只看图写的，不是剧情原文：', '']
    if blind:
        lines += [f"- event：{blind.get('event', '')}", f"- action：{blind.get('action', '')}",
                  f"- ending：{blind.get('ending', '')}"]
    else:
        lines.append('- 终审没有给出看图复述。')
    lines.append('')
    issues = []
    if (board.get('props') or any(e['shot'].get('key_props') or e['shot'].get('prop_tags') for e in board.get('shots', []))):
        issues.append('道具未经审核，只作陪衬，数量、归属、状态不保证。')
    for number in numbers:
        slot = store.get(str(number))
        if slot is not None and not _passed(slot):
            issues.append(f'第 {number} 镜所有候选都没通过筛选，交付的是按排序取到的最好候选，请人工复看。')
    issues.append('多余肢体检查只筛掉明确阳性的候选，可能漏检，请看图复核手和腿。')
    if identity.get('identity_tier') == 'L3':
        issues.append('层级 L3：底模不认识该角色，这一组用的是参考图路线（IPAdapter 参考定妆图，不用 LoRA）。')
    if 'Budget' in str(agent.get('error') or '') or 'budget' in str(agent.get('error') or ''):
        issues.append(f"预算或时间耗尽，任务提前结束：{agent.get('error')}")
    lines += ['## 已知问题', '']
    lines += [f'- {x}' for x in issues]
    lines += ['', '## 详细审核', '', '见 review.md 和 manifest.json。', '']
    return '\n'.join(lines)


def write_result_md(root, store=None):
    """Rewrite result.md of a candidate job; a no-op for jobs without candidates.json (repair jobs)."""
    root = Path(root)
    if not _store_path(root).exists():
        return
    (root / 'result.md').write_text(result_text(root, store), encoding='utf-8')


def publish_candidates(root):
    """Candidate artefacts for every delivery write: sheet at the job root and in delivery/, result.md,
    manifest records. A no-op for jobs without a candidate store (repair jobs)."""
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
            records.append({'shot': number, 'candidate': r['candidate'], 'round': r['round'], 'seed': r['seed'],
                            'attempt_number': r['attempt_number'], 'status': r['status'],
                            'image': r['image'], 'screen': r['screen'],
                            'screen_codes': [screen_code(x) for x in r['screen']], 'rank': r['rank'],
                            'conclusion': r['conclusion'], 'selected': r['candidate'] == slot['selected'],
                            'reason': slot['reason'] if r['candidate'] == slot['selected'] else None,
                            'user_pick': r['candidate'] == slot['selected'] and slot.get('user_pick') is not None,
                            'prompt_id': r.get('prompt_id')})
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
