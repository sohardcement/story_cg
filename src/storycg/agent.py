"""Hermes-facing facade over the existing restart-safe story pipeline."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import httpx
from storycg.budget import Budget, GuardedComfy, count_usage
from storycg.comfy import ComfyClient
from storycg.config import check_removed_switches, load_config
from storycg.controls import apply_options
from storycg.net_errors import RESOURCE_FAILURES, json_body
from storycg.pipeline import new_job, resolve_job, run_story_job, make_contact_sheet
from storycg.beats import BeatsError, beats_guide, character_guide, compile_beats
from storycg.state import save_json_atomic

PROJECT = Path(__file__).resolve().parents[2]
V1_RELIABILITY_NOTE = ('层级判定与身份审核的可靠依据只有发色等标为 reliable 的项目。'
                    '瞳色、配饰、整体角色：该项未经可靠审核，建议用户看图确认。')
RELIABILITY_NOTE = ('发色、视觉瞳色等身份审核以逐项 reliability 配置为准。检测项仅 reliable 的可否决；'
                    '检测弃权标注未经审核；未可靠校准的项目标注“该项未经可靠审核”，建议用户看图确认。')
PROP_SCOPE_NOTICE = '道具只作陪衬，数量、归属、状态不保证。'


def read(root, name, default=None):
    path = root/name
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})


def update(root, **fields):
    record = read(root, 'agent.json')
    prior_phase = record.get('phase')
    if fields.get('phase') in ('stopped','needs_user','completed','awaiting_approval'):
        fields.setdefault('finished_at',datetime.now(timezone.utc).isoformat())
    record.update(fields)
    save_json_atomic(root/'agent.json', record)
    if fields.get('phase') and fields['phase'] != prior_phase:
        print(f"storycg {root.name}: {fields['phase']}", file=sys.stderr)
    return record


def question(root, reason, message, options=None):
    update(root, phase='needs_user', needs_user=True, reason=reason,
           question=message, options=options or [], waiting_since=datetime.now(timezone.utc).isoformat())


def end_wait(root):
    record = read(root, 'agent.json')
    stopped=record.get('finished_at')
    if 'finished_at' not in record and record.get('phase') in ('stopped','needs_user','completed','awaiting_approval'):
        stopped=record.get('waiting_since') or datetime.fromtimestamp((root/'agent.json').stat().st_mtime,timezone.utc).isoformat()
    if stopped and not record.get('waiting_since'):
        duration=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(stopped)).total_seconds())
        update(root,paused_seconds=record.get('paused_seconds',0)+duration)
    if record.get('waiting_since'):
        duration = max(0, (datetime.now(timezone.utc)-datetime.fromisoformat(record['waiting_since'])).total_seconds())
        update(root, waiting_since=None, paused_seconds=record.get('paused_seconds', 0)+duration)
    if stopped:
        update(root,finished_at=None)


def worker_alive(root):
    pid = read(root, 'agent.json').get('worker_pid')
    if not pid:
        return False
    try:
        proc = Path('/proc')/str(pid)
        # A reaped/exited process or a reused PID must not block a new recovery.
        state = (proc/'stat').read_text().rsplit(')', 1)[1].split()[0]
        command = (proc/'cmdline').read_bytes().split(b'\0')
        return state != 'Z' and b'_worker' in command and str(root).encode() in command
    except OSError:
        return False


class ResourceUnavailable(ValueError):
    pass


# V2-35: every network or resource failure the entries answer with needs_user (see storycg/net_errors.py).
RESOURCE_ERRORS = (*RESOURCE_FAILURES, ResourceUnavailable)


def check_resources(config):
    """Check health and only the node classes needed by the configured pipeline."""
    required = [('CheckpointLoaderSimple', 'ckpt_name', config['checkpoint'])]
    for item in config['style_loras']:
        required.append(('LoraLoader', 'lora_name', item['name']))
    reference = config.get('reference_models', {})
    node_only = []
    if config.get('identity_control', {}).get('mechanism') == 'ipadapter':
        required.extend([('IPAdapterModelLoader', 'ipadapter_file', reference['ipadapter']),
                         ('CLIPVisionLoader', 'clip_name', reference['clip_vision'])])
        node_only.extend(('IPAdapterAdvanced', 'LoadImage'))
    nodes = {}
    with httpx.Client(base_url=config['comfyui_url'],
                      timeout=config['http_timeout_seconds'], trust_env=False) as client:
        response = client.get('/system_stats'); response.raise_for_status()
        for name in dict.fromkeys([row[0] for row in required] + node_only):
            response = client.get('/object_info/'+name); response.raise_for_status()
            info = json_body(response)
            if name not in info:
                raise ResourceUnavailable('所需节点缺失：'+name)
            nodes[name] = info[name]
    for node, field, filename in required:
        available = nodes[node].get('input', {}).get('required', {}).get(field, [[]])[0]
        if filename not in available:
            raise ResourceUnavailable(f'所需模型或节点缺失：{node}/{filename}')


def generate(root, config):
    check_resources(config)
    from storycg.cycle import settings
    client = GuardedComfy(ComfyClient(config['comfyui_url'], config['http_timeout_seconds']), Budget(root, settings()))
    try:
        state = run_story_job(root, config, client)
    finally:
        client.close()
    if state.get('status')=='needs_user':
        question(root,state.get('reason','identity_unsupported'),state['question'])
        return
    if (root/'manifest.json').exists():
        deliver(root)
    complete = (root/'contact_sheet.jpg').exists()
    update(root, phase='completed' if complete else 'stopped', finished_at=datetime.now(timezone.utc).isoformat())



def _ensure_identity_before_planning(root, config, identity):
    """Check the locked identity against the WAI list before planning (V2-38; V2-43b: no tier, no ComfyUI).

    Planning grades every shot against the shot vocabulary, so the identity must be final before the storyboard
    call. A character outside the WAI list stops here with needs_user, before any probe or prompt.
    """
    from storycg.tier_gate import ensure_wai_identity
    if identity.get('status') == 'needs_user':
        question(root, identity['reason'], identity['question'])
        return None
    identity = ensure_wai_identity(root, identity, config)
    if identity.get('status') == 'needs_user':
        question(root, identity.get('reason', 'identity_unsupported'), identity['question'])
        return None
    return identity

def _plan_from_beats(root, config):
    """V2-10: compile the agent-written beats file; no model call is made."""
    identity = resolve_job(root)
    if identity.get('status') != 'locked':
        raise ValueError('identity requires clarification before planning')
    job = read(root, 'job.json')
    return compile_beats(root, root/read(root, 'agent.json')['beats'], identity, job['shot_count'], config)


def beat_summaries(board, standard_tags=None):
    """V2-24 (TASK-v2 §10.1): one line per shot, built only from the compiled storyboard fields; no model call.
    V2-32: each line also names the weather and what changed from the previous shot (scene change, weather, time).
    V2-47: standard_tags (canonical set of the standard outfit, None when unknown) decides shot 1's outfit line."""
    rows, previous = [], None
    for entry in board['shots']:
        shot, scene, outfit = entry['shot'], entry['scene'], entry['outfit']
        change = (f"第 {entry['number']} 镜换装：{outfit['clothing_state']}" if outfit.get('change_at_shot') == entry['number']
                  else '换装：无（沿用上一镜）' if entry['number'] > 1 else _first_shot_outfit(entry, standard_tags))
        rows.append({'number': entry['number'], 'summary': (
            f"第{entry['number']}镜：景别 {shot['framing']}，角度 {shot['camera_angle']}；"
            f"动作 {shot['action']}；表情 {shot['expression']}；"
            f"场景 {scene['location']}（{scene['time']}）；天气 {scene.get('weather', '未写')}；"
            f"{_scene_change(scene, previous)}；{change}")})
        previous = scene
    return rows


def _first_shot_outfit(entry, standard_tags):
    """V2-47: shot 1 names its outfit only when it is not the standard outfit; unknown standard keeps the old line."""
    from storycg.tags import canonical_tag
    if standard_tags is not None and {canonical_tag(tag) for tag in entry['outfit']['tags']} != standard_tags:
        return f"第 1 镜服装：{entry['outfit']['clothing_state']}（不是标准服装）"
    return '换装：无（默认服装）'


def _standard_outfit(root):
    """V2-47: canonical standard outfit tags from the job's identity.json (no network); None when there is no identity yet."""
    from storycg.tags import canonical_tag
    if not (root/'identity.json').exists():
        return None
    return {canonical_tag(item['tag']) for item in read(root, 'identity.json').get('default_outfit_tags', [])}


def _scene_change(scene, previous):
    env = scene.get('environment_change') or '未写'
    if previous is None:
        return f'场景变化：{env}（首镜）'
    diffs = []
    if scene.get('weather') != previous.get('weather'):
        diffs.append(f"天气 {previous.get('weather', '未写')} → {scene.get('weather', '未写')}")
    if scene.get('time') != previous.get('time'):
        diffs.append(f"时间 {previous.get('time')} → {scene.get('time')}")
    return f"场景变化：{env}（" + ('，'.join(diffs) if diffs else '天气、时间同上一镜') + '）'


def make_portrait(root, config, *, redo=False):
    """V2-24: produce (or with redo, one more) portrait and stop; no shot is submitted."""
    check_resources(config)
    from storycg.cycle import settings
    client = GuardedComfy(ComfyClient(config['comfyui_url'], config['http_timeout_seconds']), Budget(root, settings()))
    try:
        state = run_story_job(root, config, client, stop_after_portrait=True, redo_portrait=redo)
    finally:
        client.close()
    if state.get('status') == 'needs_user':
        question(root, state.get('reason', 'identity_unsupported'), state['question'])
        return state
    if state.get('status') != 'portrait_ready':
        raise ResourceUnavailable('身份控制所需资源不可用：' + str(state.get('status')))
    return state


def recompile_beats(root, beats_src, config):
    """V2-24: `approve --beats FILE`. Compile the new file in place of the old storyboard; the old storyboard and
    beats file are restored if the compile fails. The portrait is not touched: its cache key is identity and style only."""
    beats_src = Path(beats_src).resolve()
    if not beats_src.is_file():
        raise ValueError(f'beats file not found: {beats_src}')
    old_board = (root/'storyboard.json').read_bytes() if (root/'storyboard.json').exists() else None
    old_beats = (root/'beats.json').read_bytes() if (root/'beats.json').exists() else None
    archive = root/'edits'/('beats-'+hashlib.sha256(beats_src.read_bytes()).hexdigest()[:12])
    archive.mkdir(parents=True, exist_ok=True)
    if old_board is not None:
        (archive/'storyboard_before.json').write_bytes(old_board)
    (root/'storyboard.json').unlink(missing_ok=True)  # compile_beats reuses an existing storyboard
    shutil.copyfile(beats_src, root/'beats.json')
    try:
        board = _plan_from_beats(root, config)
    except Exception:
        (root/'storyboard.json').unlink(missing_ok=True)
        if old_board is not None:
            (root/'storyboard.json').write_bytes(old_board)
        if old_beats is not None:
            (root/'beats.json').write_bytes(old_beats)
        raise
    update(root, beats='beats.json', beats_sha256=hashlib.sha256((root/'beats.json').read_bytes()).hexdigest(),
           storyboard=beat_summaries(board, _standard_outfit(root)))
    return board


def execute(root):
    config = read(root, 'job.json')['generation_config']
    try:
        # V2-28: a repair-era snapshot or cycle.json stops here with a clear error.
        check_removed_switches(config)
        # V2-29: the local-model planner is gone; only beats storyboards are accepted (no model call here).
        if not read(root, 'agent.json').get('beats'):
            raise ValueError('this job has no beats storyboard: the local-model planner was removed (V2-29); '
                             'create a new job with beats-guide, compile and make --beats')
        from storycg.delivery import refuse_repair_cycle
        refuse_repair_cycle(root)
        end_wait(root)
        update(root, finished_at=None, needs_user=False, reason=None, question=None, options=[], error=None)
        if not read(root, 'agent.json').get('notices'):
            update(root, notices=[PROP_SCOPE_NOTICE])
        update(root, phase='resolving_identity')
        identity = resolve_job(root)
        if identity.get('status')=='needs_user':
            question(root,identity['reason'],identity['question'])
            return
        if identity.get('status') != 'locked':
            question(root, 'identity_ambiguous', '角色身份无法确定，请选择候选或提供作品名／角色 tag。', identity.get('candidates', []))
            return
        update(root, phase='checking_identity')
        identity = _ensure_identity_before_planning(root, config, identity)
        if identity is None:
            return
        update(root, phase='planning')
        record = read(root, 'agent.json')
        # V2-24: the summary is built from the compiled storyboard; confirmation waits for the portrait too.
        board = _plan_from_beats(root, config)
        update(root, storyboard=beat_summaries(board, _standard_outfit(root)))
        if record.get('storyboard_first') and not record.get('approved'):
            update(root, phase='preparing_portrait')
            make_portrait(root, config)
            if read(root, 'agent.json').get('needs_user'):
                return
            update(root, phase='awaiting_approval', waiting_since=datetime.now(timezone.utc).isoformat())
            return
        update(root, phase='generating_and_repairing')
        generate(root, config)
    except RESOURCE_ERRORS as exc:
        question(root, 'resource_unavailable', '关键资源不可用：'+str(exc)+'。请恢复资源后运行 resume，或调整资源配置后新建任务。')
    except Exception as exc:
        # Validation/programming/budget failures are not a reason to ask the user.
        update(root, phase='stopped', needs_user=False, error=f'{type(exc).__name__}: {exc}')
        print(f'storycg: {type(exc).__name__}: {exc}', file=sys.stderr)


def _delivery_images(root, board, state, cycle):
    """The image record of every shot, from the selected (cycle best) attempts."""
    from storycg.pipeline import attempt_record
    images, drop_lines = [], []
    for e in board['shots']:
        number = e['number']; selected = cycle.get('shots', {}).get(str(number), {}).get('best_attempt')
        matches = [a for a in state['attempts'] if a['shot_id'] == f'shot{number:02d}' and a['status'] == 'completed']
        if not matches:
            raise ValueError(f'missing completed image for shot {number}')
        attempt = next((a for a in matches if a['attempt_number'] == selected), matches[-1])
        if not (root/attempt['image']['path']).is_file():
            raise ValueError(f'missing image file for shot {number}')
        # V2-52: the record is the attempt's own (prompt, seed, parameters), not a copy of the older manifest.
        images.append({**attempt_record(attempt), 'entry': e})
        dropped = attempt.get('dropped_scene_tags') or []
        if dropped:
            drop_lines.append(f'第 {number} 镜因景别物件上限省略了：{"、".join(dropped)}')
    return images, drop_lines


def pick_delivery(root, number, candidate):
    """V2-24 (TASK-v2 §10.3 换候选): swap one shot's candidate and rebuild the delivery files. No model call,
    no image, no final review: cycle.json, the contact sheet, manifest and result.md are re-derived."""
    root = Path(root)
    if not (root/'candidates.json').exists() or read(root, 'cycle.json').get('delivery_mode') != 'candidates':
        raise ValueError('pick is only for a completed candidate-mode job')
    if read(root, 'agent.json').get('phase') != 'completed':
        raise ValueError('pick requires a completed job')
    from storycg.candidates import pick_candidate, publish_candidates
    change = pick_candidate(root, number, candidate)
    cycle = read(root, 'cycle.json')
    slot = cycle['shots'][str(number)]
    slot['best_attempt'] = change['attempt_number']
    slot['best_audit'] = read(root/'cycle_artifacts', f"{change['audit_label']}.json")
    save_json_atomic(root/'cycle.json', cycle)
    board, state, manifest = read(root, 'storyboard.json'), read(root, 'state.json'), read(root, 'manifest.json')
    images, _ = _delivery_images(root, board, state, cycle)
    manifest.update(images=images, attempts=state['attempts'])
    manifest.pop('repair_history', None)  # V2-52: the old per-shot audit copy is no longer written
    save_json_atomic(root/'manifest.json', manifest)
    # Contact sheet: rebuilt in a scratch folder, then written over the job's own delivery file.
    import tempfile
    with tempfile.TemporaryDirectory(dir=root) as scratch:
        sheet_path = make_contact_sheet(root, Path(scratch))
        delivery = root/'delivery'; delivery.mkdir(exist_ok=True)
        for old in delivery.glob(f"{read(root, 'job.json')['character']}-*.jpg"):
            if old.name != sheet_path.name:
                old.unlink()
        (delivery/sheet_path.name).write_bytes(sheet_path.read_bytes())
    publish_candidates(root)
    return change


def delivery_scope_notes(root):
    """Fixed v2 delivery notice plus the saved storyboard adaptation, if any."""
    path = Path(root)/'storyboard.json'
    board = json.loads(path.read_text()) if path.exists() else {}
    note = board.get('story_note', '').strip()
    return ['道具未经审核。'] + ([note] if note else [])


def deliver(root):
    """Rebuild from persisted selected attempts only: no HTTP, generation or review."""
    board = read(root, 'storyboard.json')
    state = read(root, 'state.json')
    if not board or not state:
        raise ValueError('no generated storyboard images to deliver')
    cycle = read(root, 'cycle.json')
    manifest = read(root, 'manifest.json')
    images, drop_lines = _delivery_images(root, board, state, cycle)
    make_contact_sheet(root, root/'delivery')
    manifest.update(images=images, usage=count_usage(root), attempts=state['attempts'])
    manifest.pop('review_gate', None)  # V2-42: the group verdict is no longer recorded, also not in older manifests
    manifest.pop('repair_history', None)  # V2-52: the old per-shot audit copy is no longer written
    manifest.setdefault('identity_file', 'identity.json')
    manifest.setdefault('storyboard_file', 'storyboard.json')
    manifest.setdefault('models', {'status': 'unavailable', 'reason': 'no persisted model snapshot; offline delivery does not query models'})
    final_review = cycle.get('final_review')
    manifest['reliability_note'] = RELIABILITY_NOTE
    save_json_atomic(root/'manifest.json', manifest)
    if (root/'candidates.json').exists():
        # V2-42: a candidate job's result.md is written by publish_candidates alone; the patches below are not needed.
        from storycg.candidates import publish_candidates
        publish_candidates(root)
        return
    result = root/'result.md'
    existing = result.read_text() if result.exists() else '# 结果说明\n\n未保存可靠审核结论，建议用户看图确认。\n'
    existing = existing.replace(V1_RELIABILITY_NOTE, RELIABILITY_NOTE)
    if RELIABILITY_NOTE not in existing:
        existing += '\n'+RELIABILITY_NOTE+'\n'
    result.write_text(existing)
    from storycg.review import reliability_note_lines
    notes = reliability_note_lines(final_review, read(root, 'job.json').get('generation_config'))
    existing = result.read_text()
    scope_notes = [note for note in delivery_scope_notes(root) if note not in existing]
    if scope_notes:
        existing += '\n' + '\n'.join(scope_notes) + '\n'
        result.write_text(existing)
    missing = [note for note in notes if note not in existing.splitlines()]
    if missing:
        result.write_text(existing + '\n## 审核项说明\n\n' + '\n'.join(missing) + '\n')
    if drop_lines:
        existing = result.read_text()
        if '## 已知问题' not in existing:
            existing = existing.rstrip('\n') + '\n\n## 已知问题\n'
        new_lines = [line for line in drop_lines if line not in existing.splitlines()]
        if new_lines:
            result.write_text(existing + '\n'.join(new_lines) + '\n')
    # V2-21 rework 3: candidate sheet, its copy in delivery/, and the candidate section come last.
    from storycg.candidates import publish_candidates
    publish_candidates(root)


def status(root):
    record = read(root, 'agent.json')
    state = read(root, 'state.json')
    cycle = read(root, 'cycle.json')
    manifest = read(root, 'manifest.json')
    phase = record.get('phase', state.get('status', 'unknown'))
    active = False
    with (root/'agent.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active = True
    active = active or worker_alive(root)
    # V2-47: checking_identity is the identity check before planning; the two older names are for jobs made before V2-44b.
    if not active and phase in ('resolving_identity', 'checking_identity', 'determining_tier', 'checking_requirements',
                                'planning', 'preparing_portrait', 'generating_and_repairing'):
        phase = 'interrupted'
    paths = {'preview': str(root/'contact_sheet.jpg') if (root/'contact_sheet.jpg').exists() else None,
             'images': [str(root/x['path']) for x in manifest.get('images', [])],
             'result': str(root/'result.md') if (root/'result.md').exists() else None,
             'manifest': str(root/'manifest.json') if manifest else None}
    total = read(root, 'job.json').get('shot_count', 0)
    result = {'job_id': root.name, 'job_dir': str(root), 'phase': phase, 'active': active,
              'needs_user': record.get('needs_user', False), 'reason': record.get('reason'),
              'question': record.get('question'), 'options': record.get('options', []),
              'notices': record.get('notices', []),
              'error': record.get('error'), 'progress': _progress(root, total, cycle),
              'usage': count_usage(root),
              'stage': cycle.get('phase') if phase == 'generating_and_repairing' else phase,
              'artifacts': paths,
              'confirmation': _confirmation(root) if phase == 'awaiting_approval' else None}
    if phase == 'awaiting_approval':
        # V2-41: the storyboard summary is shown to the user only at awaiting_approval.
        result['storyboard'] = _summary_for_status(root, record, phase)
    if phase == 'identity_confirmed':
        # V2-34: after resolve the status names the locked character and the next step, not the old question.
        identity = read(root, 'identity.json')
        result['locked_identity'] = {'character_tag': identity.get('character_tag'), 'work_tag': identity.get('work_tag')}
        result['next_step'] = (f"身份已锁定为 {identity.get('character_tag')}（作品 {identity.get('work_tag')}）："
                               '运行 resume 继续定档与出图。')
    return result


def _summary_for_status(root, record, phase):
    """V2-32: at awaiting_approval the summary is rebuilt from storyboard.json, so a job made before a summary
    change shows the current lines. Older storyboards without the beats fields keep the stored summary."""
    stored = record.get('storyboard', [])
    if phase != 'awaiting_approval' or not (root/'storyboard.json').exists():
        return stored
    try:
        return beat_summaries(read(root, 'storyboard.json'), _standard_outfit(root))
    except (KeyError, TypeError):
        return stored


def _confirmation(root):
    """V2-24: what the user confirms at awaiting_approval: portrait, candidates per shot.
    V2-32: also the storyboard's story_note, the compile's automatic rewrites and the raw portrait review."""
    from storycg.config import candidates_per_shot
    from storycg.display import compile_rewrites, portrait_review
    portrait = read(root, 'portrait.json')
    config = read(root, 'job.json').get('generation_config', {})
    return {'portrait_status': portrait.get('raw_status', portrait.get('status')),
            'portrait_image': str(root/portrait['image']['path']) if portrait.get('image') else None,
            'portrait_face': str(root/portrait['face']['path']) if portrait.get('face') else None,
            'portrait_attempts': len(read(root, 'portrait_attempts.json', []) or []),
            'portrait_review': portrait_review(root),
            'story_note': read(root, 'storyboard.json').get('story_note', ''),
            'compile_rewrites': compile_rewrites(root),
            'candidates_per_shot': candidates_per_shot(config)}


def _is_candidate_job(job_config):
    """V2-33: a candidate-delivery job is one whose own config snapshot carries candidates_per_shot (every job
    made in the candidate era does; the repair-era snapshots do not). Decided by the config, never by whether
    candidates.json exists yet."""
    return 'candidates_per_shot' in (job_config or {})


def _generated_candidates(state):
    """V2-34: the completed images per shot, counted from the submission ledger (state.json attempts), one per
    candidate number. candidates.json gets a record only after the audit, so it lags behind a finished image."""
    done = {}
    for attempt in state.get('attempts', []):
        if attempt.get('status') == 'completed' and str(attempt.get('shot_id', '')).startswith('shot') \
                and attempt.get('candidate') is not None:
            done.setdefault(attempt['shot_id'], set()).add(attempt['candidate'])
    return done


def _progress(root, total, cycle):
    """V2-32: stage by stage. completed_shots is the number of shots with a picked candidate (not shots with
    one finished image). V2-33: in a candidate-delivery job completed_shots is always the picked count, and
    before candidates.json is written every shot is 0 generated / 0 picked. Only jobs whose config has no
    candidate delivery (repair era) keep the attempt-based count. V2-34: candidates_generated comes from
    state.json; reviewed and picked still come from candidates.json."""
    from storycg.config import candidates_per_shot
    job_config = read(root, 'job.json').get('generation_config', {})
    if not _is_candidate_job(job_config):
        state = read(root, 'state.json')
        done = len({a['shot_id'] for a in state.get('attempts', []) if a['status'] == 'completed' and a['shot_id'].startswith('shot')})
        return {'completed_shots': done, 'total_shots': total, 'shots': [], 'final_review_done': cycle.get('phase') == 'complete'}
    store = read(root, 'candidates.json', {})
    generated = _generated_candidates(read(root, 'state.json'))
    per = candidates_per_shot(job_config)
    shots = []
    for number in range(1, total + 1):
        slot = store.get(str(number), {})
        cands = slot.get('candidates', [])
        shots.append({'shot': number,
                      'candidates_generated': len(generated.get(f'shot{number:02d}', set())),
                      'candidates_expected': per * slot.get('rounds', 1),
                      # V2-61: a screened candidate is marked checked (records before V2-61 carry a conclusion).
                      'candidates_reviewed': sum(1 for r in cands if r.get('checked') or r.get('conclusion') is not None),
                      'picked': slot.get('selected') is not None})
    return {'completed_shots': sum(1 for s in shots if s['picked']), 'total_shots': total, 'shots': shots,
            'final_review_done': cycle.get('phase') == 'complete'}


def launch(root):
    with (root/'worker.log').open('a') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'storycg', '_worker', str(root)],
                                cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    update(root, worker_pid=proc.pid)


def _resource_failure(exc):
    """V2-33: the same needs_user wording make and status give for a missing resource, printed by compile."""
    return {'status': 'needs_user', 'needs_user': True, 'reason': 'resource_unavailable',
            'question': '关键资源不可用：' + str(exc) + '。请恢复后重试当前命令。', 'options': []}


def beats_cli(argv):
    """`beats-guide` prints the writing guide; `compile` checks a beats file without generating images."""
    parser = argparse.ArgumentParser(prog='storycg')
    sub = parser.add_subparsers(dest='command', required=True)
    guide = sub.add_parser('beats-guide', help='print the story beats writing guide')
    guide.add_argument('--shots', type=int, default=4)
    guide.add_argument('--character', help='also print the standard outfit tags and the vocabulary for this character')
    guide.add_argument('--config', type=Path, default=PROJECT/'config/default.json')
    comp = sub.add_parser('compile', help='compile a beats file; reports every error; no image generation')
    comp.add_argument('--character', required=True)
    comp.add_argument('--beats', required=True, type=Path)
    comp.add_argument('--shots', type=int, default=4)
    comp.add_argument('--config', type=Path, default=PROJECT/'config/default.json')
    args = parser.parse_args(argv)
    if args.command == 'beats-guide':
        info = None
        if args.character:
            from storycg.identity import resolve_identity
            config = load_config(args.config)
            try:
                identity = resolve_identity(args.character, PROJECT/'cache'/'danbooru')
            except RESOURCE_ERRORS as exc:
                print(json.dumps(_resource_failure(exc), ensure_ascii=False, indent=2))
                return 1
            info = character_guide(identity, config)
            if info['unsupported']:
                print(info['unsupported'])
                return 2
        print(beats_guide(args.shots, info))
        return 0
    config = load_config(args.config)
    # Scratch job directory: the tier and planning code expect a job.json; nothing here generates images.
    root = PROJECT/'compile'/('cmp-'+uuid.uuid4().hex[:12])
    new_job(root, args.character, 'beats compile check', args.shots, generation_config=config)
    try:
        identity = resolve_job(root)
        if identity.get('status') != 'locked':
            print(json.dumps({'status': 'identity_not_locked', 'identity': identity}, ensure_ascii=False, indent=2))
            return 2
        identity = _ensure_identity_before_planning(root, config, identity)
    except RESOURCE_ERRORS as exc:
        # V2-33: a Danbooru 500 or a lost connection is a needs_user answer, as in make, not a traceback.
        print(json.dumps(_resource_failure(exc), ensure_ascii=False, indent=2))
        return 1
    if identity is None:
        record = read(root, 'agent.json')
        print(json.dumps({'status': 'needs_user', 'reason': record.get('reason'), 'question': record.get('question')},
                         ensure_ascii=False, indent=2))
        return 2
    guide = character_guide(identity, config)
    try:
        board = compile_beats(root, args.beats, identity, args.shots, config)
    except BeatsError as exc:
        print(json.dumps({'status': 'invalid', 'errors': exc.errors, 'message': str(exc),
                          'character_guide': guide}, ensure_ascii=False, indent=2))
        return 2
    except RESOURCE_ERRORS as exc:
        # The tag existence checks call Danbooru during compile; the same failure as above.
        print(json.dumps(_resource_failure(exc), ensure_ascii=False, indent=2))
        return 1
    from storycg.display import compile_rewrites
    # V2-51 (D88): every rewrite of this compile, the automatic ones and the removed or replaced tags.
    print(json.dumps({'status': 'valid',
                      'storyboard': board, 'job_dir': str(root), 'character_guide': guide,
                      'rewrites': compile_rewrites(root)},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv):
    if argv and argv[0] in ('beats-guide', 'compile'):
        return beats_cli(argv)
    parser = argparse.ArgumentParser(prog='storycg')
    sub = parser.add_subparsers(dest='command', required=True)
    # Help-only entries: beats-guide and compile are dispatched before this parser runs.
    sub.add_parser('beats-guide', help='print the story beats writing guide (--character adds outfit tags and vocabulary)')
    sub.add_parser('compile', help='check a beats file without generating images (--character --beats FILE)')
    for name in ('make', 'approve', 'status', 'resume', 'deliver', 'pick', 'resolve', '_worker'):
        command = sub.add_parser(name)
        command.add_argument('job_dir', nargs='?' if name == 'make' else None, type=Path)
        if name in ('make', 'approve', 'resume'):
            command.add_argument('--foreground', action='store_true')
        if name == 'make':
            command.add_argument('--beats', type=Path, help='required: the beats file written with beats-guide')
            command.add_argument('--character', required=True)
            command.add_argument('--story', required=True)
            command.add_argument('--shots', type=int, default=4)
            command.add_argument('--no-confirm', action='store_true',
                                 help='beats mode stops for confirmation by default; this runs straight through')
            command.add_argument('--config', type=Path, default=PROJECT/'config/default.json')
            command.add_argument('--identity-control', choices=['none', 'ipadapter'])
            command.add_argument('--seed', type=int)
        if name in ('make','resume'):
            command.add_argument('--tag')
            command.add_argument('--work')
        if name == 'resolve':
            command.add_argument('--tag', help='explicit canonical character tag')
            command.add_argument('--work', help='work name or copyright tag for disambiguation')
        if name == 'approve':
            command.add_argument('--beats', type=Path, help='recompile with a new beats file; stays awaiting_approval')
            command.add_argument('--redo-portrait', action='store_true', help='one more portrait; stays awaiting_approval')
        if name == 'pick':
            command.add_argument('--shot', type=int)
            command.add_argument('--candidate', type=int)
    args = parser.parse_args(argv)
    root = (args.job_dir or PROJECT/'jobs'/('cg-'+uuid.uuid4().hex[:12])).resolve()
    code = 0
    picked = None
    resolved = None
    try:
        if not root.is_relative_to(PROJECT):
            raise ValueError('agent job paths must be inside the project workspace')
        if args.command == 'make':
            if args.beats is None:
                raise ValueError('make needs --beats: write the storyboard with beats-guide, check it with compile, then run make --beats FILE')
            options = {k: v for k, v in {'identity_control': args.identity_control, 'generation_seed': args.seed,'character_tag':args.tag,'work':args.work}.items() if v is not None}
            config = apply_options(load_config(args.config), options)
            if (root/'agent.json').exists():
                raise ValueError('job already exists; use status/resume/approve')
            beats_src = args.beats.resolve()
            if not beats_src.is_file():
                raise ValueError(f'beats file not found: {args.beats}')
            new_job(root, args.character, args.story, args.shots, generation_options=options, generation_config=config)
            shutil.copyfile(beats_src, root/'beats.json')
            beats_fields = {'beats': 'beats.json', 'beats_sha256': hashlib.sha256((root/'beats.json').read_bytes()).hexdigest()}
            # V2-24: beats mode confirms by default; --no-confirm runs straight through.
            confirm = not args.no_confirm
            update(root, phase='queued', needs_user=False, storyboard_first=confirm, approved=False, **beats_fields)
        elif not (root/'job.json').exists():
            raise ValueError('story job not found')
        if args.command != 'status':
            with (root/'agent.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX if args.command == '_worker' else fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('job worker is already active; poll status')
                if args.command != '_worker' and worker_alive(root):
                    raise ValueError('job worker is starting or active; poll status')
                if args.command=='resume' and any((args.tag,args.work)):
                    if read(root,'state.json').get('attempts'):
                        raise ValueError('cannot change identity inputs after story submission')
                    job=read(root,'job.json');opts=job.get('generation_options',{})
                    old=read(root,'identity.json')
                    from storycg.tier_gate import wai_locked
                    if wai_locked(old) and (
                            (args.tag and args.tag!=old.get('character_tag')) or
                            (args.work and args.work!=old.get('work_tag'))):
                        raise ValueError('identity already verified; create a new job for different identity inputs')
                    opts=job.setdefault('generation_options',{})
                    opts.update({k:v for k,v in {'character_tag':args.tag,'work':args.work}.items() if v})
                    save_json_atomic(root/'job.json',job)
                    old=read(root,'identity.json')
                    if old.get('status')=='needs_user':
                        save_json_atomic(root/'identity-before-user-input.json',old)
                        if args.tag or args.work:
                            from storycg.identity import resolve_identity
                            prior=old
                            old=resolve_identity(job['character'],root/'cache/danbooru',character_tag=args.tag,work=args.work)
                            if any(prior.get(key)!=old.get(key) for key in ('character_tag','identity_block','default_outfit_tags')):
                                digest=hashlib.sha256(json.dumps(prior,sort_keys=True).encode()).hexdigest()[:16]
                                archive=root/'edits'/('identity-'+digest);archive.mkdir(parents=True,exist_ok=True)
                                for name in ('storyboard.json','plan_log.json'):
                                    if (root/name).exists():shutil.move(root/name,archive/name)
                                update(root,storyboard=[],approved=False)
                        elif old.get('character_tag'):
                            old.update(status='locked',needs_user=False)
                        save_json_atomic(root/'identity.json',old)
                if args.command == 'deliver':
                    deliver(root)
                elif args.command == 'pick':
                    if args.shot is None or args.candidate is None:
                        raise ValueError('pick needs --shot and --candidate')
                    picked = pick_delivery(root, args.shot, args.candidate)
                elif args.command == 'resolve':
                    # Identity choice (V2-29: kept for the SKILL's ambiguity step); a locked result is required before resume.
                    resolved = resolve_job(root, work=args.work, character_tag=args.tag)
                    record = read(root, 'agent.json')
                    if resolved.get('status') == 'locked' and record.get('phase') == 'needs_user' \
                            and record.get('reason') == 'identity_ambiguous':
                        # V2-34: the ambiguity question is answered; drop it so status stops repeating it.
                        end_wait(root)
                        update(root, phase='identity_confirmed', needs_user=False, reason=None,
                               question=None, options=[], error=None)
                elif args.command == 'approve' and (args.beats is not None or args.redo_portrait):
                    # V2-24: confirmation steps that stay at awaiting_approval.
                    record = read(root, 'agent.json')
                    if record.get('phase') != 'awaiting_approval':
                        raise ValueError('approve requires awaiting_approval')
                    if args.beats is not None and not record.get('beats'):
                        raise ValueError('approve --beats is only for beats-mode jobs')
                    job_config = read(root, 'job.json')['generation_config']
                    check_removed_switches(job_config)
                    if args.beats is not None:
                        recompile_beats(root, args.beats, job_config)
                    if args.redo_portrait:
                        make_portrait(root, job_config, redo=True)
                elif args.command == 'approve':
                    if read(root, 'agent.json').get('phase') != 'awaiting_approval':
                        raise ValueError('approve requires awaiting_approval')
                    end_wait(root)
                    update(root, approved=True, phase='queued')
                    if args.foreground:
                        with contextlib.redirect_stdout(sys.stderr):
                            execute(root)
                    else:
                        launch(root)
                elif args.command == 'resume' and read(root, 'agent.json').get('phase') in ('awaiting_approval', 'completed'):
                    pass
                elif args.command == '_worker' or getattr(args, 'foreground', False):
                    with contextlib.redirect_stdout(sys.stderr):
                        execute(root)
                else:
                    update(root, phase='queued')
                    launch(root)
        result = status(root)
        if picked is not None:
            result['pick'] = picked
        if resolved is not None:
            result['resolve'] = resolved
            if resolved.get('status') != 'locked':
                code = 2
        if result.get('error'):
            code = 1
    except Exception as exc:
        code = 1
        print(f'storycg: {type(exc).__name__}: {exc}', file=sys.stderr)
        result = status(root) if root.is_relative_to(PROJECT) and (root/'job.json').exists() else {'job_dir': str(root), 'needs_user': False}
        if root.is_relative_to(PROJECT) and (root/'job.json').exists() and isinstance(exc, RESOURCE_ERRORS):
            record = read(root, 'agent.json')
            question(root, 'resource_unavailable', '关键资源不可用：'+str(exc)+'。请恢复后重试当前命令。')
            if args.command == 'approve':
                update(root, phase='awaiting_approval')
            result = status(root)
        result['error'] = f'{type(exc).__name__}: {exc}'
    if args.command in ('make', 'resolve') and result.get('needs_user') and result.get('reason') == 'resource_unavailable':
        code = 1  # V2-35: a missing network or resource ends make/resolve with the needs_user JSON and exit 1
    if args.command != '_worker':
        print(json.dumps(result, ensure_ascii=False))
    return code
