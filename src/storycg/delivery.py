"""Candidate delivery from persisted files: manifest, contact sheet, result.md and the group gate.

V2-28: moved here from cycle.deliver and runtime_gate.deliver when the repair delivery was removed.
The steps and their order are unchanged; only lines that the repair loop alone could produce are gone.
"""
from storycg import review
from storycg.config import RemovedMode
from storycg.cycle import issues, read, selected_attempt, usage
from storycg.state import load_state, save_json_atomic


def deliver(root, cycle, config, client):
    from storycg.pipeline import write_story_manifest, make_contact_sheet
    from storycg.review_gate import persist_group_gate
    from storycg.v1_scope import render_delivery
    selected_board = read(root/'storyboard.json')
    if not (root/'initial_storyboard.json').exists():
        save_json_atomic(root/'initial_storyboard.json', selected_board)
    selected_board['shots'] = [slot['entry'] for slot in cycle['shots'].values()]
    save_json_atomic(root/'storyboard.json', selected_board)
    manifest = write_story_manifest(root, config, client); state = load_state(root)
    manifest['images'] = []; lines = ['# 结果说明', '']
    notes = review.reliability_note_lines(cycle.get('final_review'), config)
    if notes:
        lines.extend(['## 审核项说明', '', *notes, ''])
    human_path = root/'human_observations.json'
    human = read(human_path) if human_path.exists() else {}
    portrait_path = root/'portrait.json'
    if portrait_path.exists():
        lines.extend(read(portrait_path).get('gate_notes', []))
    for number, slot in cycle['shots'].items():
        a = selected_attempt(state, int(number), slot['best_attempt'])
        manifest['images'].append({'shot_id': a['shot_id'], 'attempt_number': a['attempt_number'], **a['image'], 'entry': slot['entry']})
        audit = slot['best_audit']
        final = cycle.get('final_review', {})
        singles = [r for r in final.get('single', []) if r['shot'] == int(number)]
        consistencies = [r for r in final.get('consistency', []) if r['shot'] == int(number)]
        faces = [r for r in final.get('faces', []) if r['shot'] == int(number)]
        if singles and consistencies:
            audit = {**audit, 'single': singles[0], 'consistency': consistencies[0],
                     'conclusion': review.conclude([*review.result_statuses(singles[0]), *review.result_statuses(consistencies[0]),
                                                    *[r['status'] for r in faces]])}
        slot['final_conclusion'] = audit['conclusion']
        lines.append(f'## 第{number}镜：{audit["conclusion"]}')
        lines.append(f'停止原因：{slot.get("stop", "pending")}；选用 attempt {slot["best_attempt"]}。')
        for issue in issues([audit['single'], audit['consistency']]):
            note = '' if issue.get('reliability') == 'reliable' else '；该项未经可靠审核'
            lines.append(f'- {issue["key"]}: {issue["status"]} — {issue["reason"]}{note}')
        unreliable = [k for r in [audit['single'], audit['consistency']] for k, v in r.get('data', {}).items() if v.get('reliability') != 'reliable']
        lines.append('该项未经可靠审核：' + ', '.join(unreliable))
        from storycg import extra_part
        if extra_part.is_positive(audit):
            lines.append(extra_part.known_issue(int(number), audit))
        for observation in human.get(number, []):
            lines.append('- 人眼已知问题（不改冻结结论）：' + observation)
        lines.append('')
    manifest['usage'] = usage(root); manifest['repair_cycle_file'] = 'cycle.json'; manifest['repair_history'] = cycle['shots']
    manifest['attempts'] = state['attempts']
    if cycle.get('final_review'):
        lines.append('整组冻结审核：' + cycle['final_review']['conclusion'])
    final = cycle.get('final_review', {})
    if final:
        blind = final.get('narrative_blind', {})
        lines.append('盲审调用：' + blind.get('status', 'unknown') + '；' + blind.get('error', ''))
        for key, item in final.get('narrative_compare', {}).get('data', {}).items():
            if item.get('status') == 'fail':
                lines.append('- 盲审 ' + key + '：' + item.get('reason', ''))
    save_json_atomic(root/'manifest.json', manifest)
    (root/'result.md').write_text('\n'.join(lines) + '\n')
    make_contact_sheet(root, root.parent.parent/'review')
    if cycle.get('final_review'):
        gate = persist_group_gate(root, cycle['final_review'], config)
        manifest = read(root/'manifest.json'); manifest['review_gate'] = gate
        save_json_atomic(root/'manifest.json', manifest)
        render_delivery(root, cycle['final_review'], gate, config)
    # V2-21 rework 3: every rewrite of result.md keeps the candidate section.
    from storycg.candidates import publish_candidates
    publish_candidates(root)
    return state


def refuse_repair_cycle(root):
    """V2-28: a cycle.json written by the removed repair delivery cannot be run any more."""
    path = root/'cycle.json'
    if path.exists() and read(path).get('delivery_mode') != 'candidates':
        raise RemovedMode('修图方式已删除（V2-28）：这个任务是修图方式的旧任务（cycle.json 不是候选交付），'
                          '不能再继续运行；请新建任务。已有交付物仍可用 deliver 重新生成。')


def deliver_completed(root, client):
    """A job whose candidate selection already finished (cycle.json complete) is delivered again.

    This is the resume path of a finished candidate job. A cycle.json written by the removed repair
    delivery is refused with a clear error instead of being run another way.
    """
    from storycg.budget import Budget, GuardedComfy
    refuse_repair_cycle(root)
    cycle = read(root/'cycle.json')
    if cycle.get('phase') != 'complete':
        raise ValueError('candidate cycle.json is not complete: ' + str(cycle.get('phase')))
    config = load_state(root)['config']
    return deliver(root, cycle, config, GuardedComfy(client, Budget(root, cycle['config'])))
