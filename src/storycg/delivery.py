"""Candidate delivery from persisted files: manifest, contact sheet and the candidate result.md.

V2-28: moved here from cycle.deliver and runtime_gate.deliver when the repair delivery was removed.
V2-42: the group verdict (review_gate.json, render_delivery) and the per-shot result.md lines that
publish_candidates rewrote anyway are gone; result.md is written only by publish_candidates.
"""
from storycg import review
from storycg.config import RemovedMode
from storycg.cycle import read, selected_attempt, usage
from storycg.state import load_state, save_json_atomic


def deliver(root, cycle, config, client):
    from storycg.pipeline import attempt_record, write_story_manifest, make_contact_sheet
    selected_board = read(root/'storyboard.json')
    if not (root/'initial_storyboard.json').exists():
        save_json_atomic(root/'initial_storyboard.json', selected_board)
    selected_board['shots'] = [slot['entry'] for slot in cycle['shots'].values()]
    save_json_atomic(root/'storyboard.json', selected_board)
    manifest = write_story_manifest(root, config, client); state = load_state(root)
    manifest['images'] = []
    final = cycle.get('final_review', {})
    for number, slot in cycle['shots'].items():
        a = selected_attempt(state, int(number), slot['best_attempt'])
        manifest['images'].append({**attempt_record(a), 'entry': slot['entry']})
        audit = slot['best_audit']
        consistencies = [r for r in final.get('consistency', []) if r['shot'] == int(number)]
        faces = [r for r in final.get('faces', []) if r['shot'] == int(number)]
        if consistencies:
            audit = {**audit, 'consistency': consistencies[0],
                     'conclusion': review.conclude([*review.result_statuses(audit['single']), *review.result_statuses(consistencies[0]),
                                                    *[r['status'] for r in faces]])}
        slot['final_conclusion'] = audit['conclusion']
    # V2-52: the per-shot audit copy (repair_history) is not written; cycle.json holds the same records.
    manifest['usage'] = usage(root); manifest['repair_cycle_file'] = 'cycle.json'
    manifest['attempts'] = state['attempts']
    save_json_atomic(root/'manifest.json', manifest)
    make_contact_sheet(root)
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
