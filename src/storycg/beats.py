"""Compile an agent-written story beats file into a storyboard (V2-10, TASK-v2 §9).

The beats file uses the planner JSON format (planner_schema(production_schema())).
Compiling never calls a model: it runs the same checks as plan_story after the
model response, in the same order, and reports every error it can find with its
shot number and field instead of stopping at the first one.
"""
import copy
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator

from storycg.state import save_json_atomic

OBSCURED_TAG = re.compile(r'back view|from behind|covered face|hidden face|face out of frame')
ACTION_FIELDS = {'pose', 'action', 'description', 'spatial_relation', 'change_from_previous', 'tags'}


class BeatsError(ValueError):
    """Carries every compile error; str() lists them one per line."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f'beats 编译失败，共 {len(errors)} 条错误：\n' + '\n'.join(format_error(e) for e in errors))


def format_error(error):
    where = f"第 {error['shot']} 镜" if error.get('shot') else '全局'
    return f"- {where}，字段 {error.get('field') or '-'}：{error['problem']}。修改：{error['fix']}"


def _add(errors, shot, field, problem, fix):
    errors.append({'shot': shot, 'field': field, 'problem': problem, 'fix': fix})


def _shot_of_path(path):
    """Map a jsonschema path such as ('shots', 2, 'shot', 'framing') to (3, 'shot.framing')."""
    parts = list(path)
    if len(parts) >= 2 and parts[0] == 'shots' and isinstance(parts[1], int):
        return parts[1] + 1, '.'.join(str(p) for p in parts[2:]) or 'shots'
    return None, '.'.join(str(p) for p in parts) or '-'


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _obscured(entry):
    shot = entry['shot']
    tags = ' '.join([*shot['tags'], *entry.get('scene', {}).get('tags', [])]).replace('_', ' ').lower()
    return (shot.get('subject_visibility') == 'obscured' or shot['camera_angle'] == 'behind'
            or bool(OBSCURED_TAG.search(tags)))


def _pose_key(pose):
    return '_'.join(str(pose).strip().lower().split())


def _set_combo(shot, framing, angle, pose):
    """Write a combo the way materialize derives it (pose drives action, description and tags)."""
    from storycg.shot_contract import shot_tags
    # V2-51 (D88): the action tag survives a rewrite; the pose may change, so the tags are rebuilt from both.
    shot.update(framing=framing, camera_angle=angle, pose=pose, action=pose, description=pose,
                tags=shot_tags(pose, shot.get('action_tag'), shot['expression']))


def _append_note(board, sentence):
    note = board.get('story_note', '')
    board['story_note'] = (note + ' ' if note else '') + sentence


def tag_rewrites(records):
    """V2-51 (D88): the tags validate_tag_lists discarded or replaced, as rewrite records (same keys as rewrite_board).

    records are the registry lookups of that validation. Records with the same action, field and tag (and the same
    replacement) become one record; its shots are listed in order, joined with 、 in the text.
    """
    groups = {}
    for record in records:
        action = record.get('action')
        match = re.match(r'^(\d+)\.(.+)$', record.get('field') or '')
        if action not in ('discarded', 'pose_replaced', 'pose_omitted') or not match:
            continue
        number, field = int(match.group(1)), match.group(2)
        if action != 'discarded':
            field = 'shot.pose'
        key = (action, field, record['input'], record.get('replacement'))
        groups.setdefault(key, []).append(number)
    rewrites = []
    for (action, field, tag, replacement), numbers in groups.items():
        numbers = sorted(set(numbers))
        where = '、'.join(str(n) for n in numbers)
        if action == 'discarded':
            rule, to = 'tag_unavailable', None
            text = f'第 {where} 镜：{field} 的 {tag} → 删除，Danbooru 上不存在或已废弃'
            reason = 'Danbooru 上不存在或已废弃'
        elif action == 'pose_replaced':
            rule, to = 'pose_replaced', replacement
            text = f'第 {where} 镜：shot.pose 的 {tag} → {replacement}，Danbooru 上不存在，换成同词的体位 tag'
            reason = 'Danbooru 上不存在，换成同词的体位 tag'
        else:
            rule, to = 'pose_omitted', None
            text = f'第 {where} 镜：shot.pose 的 {tag} → 不进提示词，Danbooru 上不存在，也没有可替换的体位 tag'
            reason = 'Danbooru 上不存在，也没有可替换的体位 tag'
        rewrites.append({'shot': numbers[0], 'shots': numbers, 'rule': rule, 'field': field, 'from': tag,
                         'to': to, 'reason': reason, 'text': text})
    return rewrites


def rewrite_board(board, config):
    """Deterministic rewrites before validation (V2-11, TASK-v2 §9). Mutates and returns board.

    Returns (board, rewrites). Each rewrite is logged as "第 N 镜：原值 → 新值，原因" and
    a Chinese sentence is appended to story_note. Rules: off-table pose to the stable
    combo (1), unusable combo to its registered substitute id (2), sometimes-overflow
    convergence from the back (3), indoor precipitation gets a window (5). Clothing (6)
    and the substitute-without-id error are validation errors, see _collect_errors.
    """
    from storycg.prompt_rules import indoor_precipitation
    from storycg.shot_vocab import classify_shots, grade_of, load_vocab, replacement_for
    from storycg.tags import canonical_tag

    vocab = load_vocab(config)
    items = vocab['items']
    by_id = {item['id']: item for item in items}
    rewrites = []

    def log(shot_no, rule, field, before, after, reason):
        text = f'第 {shot_no} 镜：{before} → {after}，{reason}'
        rewrites.append({'shot': shot_no, 'rule': rule, 'field': field, 'from': before, 'to': after,
                         'reason': reason, 'text': text})
        _append_note(board, text + '。')

    def _hits(all_items, shot):
        # Same matching as classify_shots: any action tag, same framing and angle.
        action = _pose_key(shot['pose'])
        return [item for item in all_items if action in [_pose_key(a) for a in item['action_tags']]
                and item['framing'] == shot['framing'] and item['camera_angle'] == shot['camera_angle']]

    def combo(shot):
        return f"{shot['framing']}/{shot['camera_angle']}/{shot['pose']}"

    def stable_candidates(pose, shot_index, shot):
        """Stable entries for this pose; the first shot may only use wide. Order: framing, angle, id."""
        found = [item for item in items if _pose_key(pose) in
                 [_pose_key(a) for a in item['action_tags']]
                 and grade_of(item) == 'stable']
        if shot_index == 0:
            found = [item for item in found if item['framing'] == 'wide']
        return sorted(found, key=lambda item: (item['framing'] != shot['framing'],
                                               item['camera_angle'] != shot['camera_angle'], item['id']))

    shots = board['shots']
    # Rule 1: an off-table combo takes the best stable entry for its pose.
    for index, entry in enumerate(shots):
        shot = entry['shot']
        hits = _hits(items, shot)
        if hits:
            continue
        candidates = stable_candidates(shot['pose'], index, shot)
        if not candidates:
            continue
        before = combo(shot)
        best = candidates[0]
        _set_combo(shot, best['framing'], best['camera_angle'], shot['pose'])
        log(entry['number'], 'off_table_stable', 'shot.framing, shot.camera_angle',
            before, combo(shot), '表外组合改用稳定组合（同一动作）')

    # Rule 2: an unusable combo with a registered substitute id takes that entry's combo.
    for index, entry in enumerate(shots):
        shot = entry['shot']
        hits = _hits(items, shot)
        unusable = [item for item in hits if grade_of(item) == 'unusable']
        if not unusable:
            continue
        row = replacement_for(unusable[0])
        if not row or row.get('substitute_id') is None or row['substitute_id'] not in by_id:
            continue  # rewrite-only or unknown substitute: the validation error reports the text
        substitute = by_id[row['substitute_id']]
        if index == 0 and substitute['framing'] != 'wide':
            continue  # the first shot must stay wide
        if not substitute['action_tags']:
            continue
        before = combo(shot)
        _set_combo(shot, substitute['framing'], substitute['camera_angle'], _pose_key(substitute['action_tags'][0]))
        log(entry['number'], 'unusable_substitute', 'shot.framing, shot.camera_angle, shot.pose',
            before, combo(shot), f"不可用组合按登记替代 {substitute['id']} 改写")

    # Rule 3: more than one sometimes shot, convert from the back (never the first shot).
    def sometimes_shots():
        return [c for c in classify_shots(board, vocab=vocab) if c['grade'] == 'sometimes']

    if len(sometimes_shots()) > 1:
        for index in range(len(shots) - 1, 0, -1):
            if len(sometimes_shots()) <= 1:
                break
            entry = shots[index]
            shot = entry['shot']
            hits = [c for c in classify_shots({'shots': [entry]}, vocab=vocab)[:1]
                    if c['grade'] == 'sometimes' and not c['off_table']]
            if not hits:
                continue
            candidates = stable_candidates(shot['pose'], index, shot)
            # A candidate with the shot's own framing and angle changes nothing (V2-12: such a
            # rewrite would only log a no-op and leave the grade unchanged).
            candidates = [c for c in candidates if (c['framing'], c['camera_angle']) != (shot['framing'], shot['camera_angle'])]
            if not candidates:
                continue
            before = combo(shot)
            best = candidates[0]
            _set_combo(shot, best['framing'], best['camera_angle'], shot['pose'])
            log(entry['number'], 'sometimes_overflow', 'shot.framing, shot.camera_angle',
                before, combo(shot), '有时组合超额，改用同一动作的稳定组合')

    # Rule 5: indoor precipitation gets a window (budget-exempt in the filter, so it stays in closeups).
    scene_rules = (config or {}).get('scene_rules') or {}
    if scene_rules:
        for entry in shots:
            tags = [canonical_tag(tag) for tag in entry['scene']['tags']]
            if 'window' in tags or not indoor_precipitation(tags, scene_rules):
                continue
            entry['scene']['tags'] = [*entry['scene']['tags'], 'window']
            log(entry['number'], 'indoor_window', 'scene.tags', 'scene.tags 无 window',
                'scene.tags 加 window', '室内场景出现降水 tag，补窗户')
    return board, rewrites


# V2-47 (supervisor, 2026-10-12): tags that are not a garment and so do not count as a new outfit. Source: the V2-47
# regression (bath and blanket shots). Danbooru has no usable coloured towel tag (white_towel and pink_towel are
# deprecated), so these cannot carry a colour. Add nothing else here without a supervisor order.
NOT_NEW_OUTFIT_TAGS = frozenset({'towel', 'bath_towel', 'naked_towel', 'barefoot', 'blanket'})


def _new_outfit_colour_errors(shots, identity):
    """V2-47: a segment of shots in a new outfit must name a colour in one of its outfit tags.

    Segments split where the canonical outfit tag set changes (the same rule as change_at_shot). A segment's new tags
    are its tags that are not in the standard outfit and not in NOT_NEW_OUTFIT_TAGS; a segment with no new tag, or a
    character with no standard outfit, is not checked. Reported once per segment, on its first shot.
    """
    from storycg.tags import canonical_tag, color_split
    standard = {canonical_tag(item['tag']) for item in (identity or {}).get('default_outfit_tags', [])}
    if not standard:
        return []
    segments = []
    for index, entry in enumerate(shots):
        tags = [canonical_tag(tag) for tag in entry['outfit']['tags']]
        if not segments or set(tags) != set(segments[-1][1]):
            segments.append((index + 1, tags))
    errors = []
    for number, tags in segments:
        new = list(dict.fromkeys(tag for tag in tags if tag not in standard and tag not in NOT_NEW_OUTFIT_TAGS))
        if new and not any(color_split(tag) for tag in new):
            _add(errors, number, 'outfit.tags',
                 f'第 {number} 镜换上的新衣服没有写颜色（{", ".join(new)}）：只写款式时模型容易画回角色自己的衣服，前后镜的颜色也会变',
                 '给主要的新衣服写一个带颜色、并且 Danbooru 上存在的 tag，比如 brown_coat、pink_pajamas，颜色最好和标准服装不同；同一套衣服的各镜用同一组 tag')
    return errors


def _action_tag_errors(errors, number, entry, tag_registry):
    """V2-49 (D87): shot.action_tag must add an action the pose, outfit, scene and props do not already carry."""
    from storycg.planning import appearance_forbidden
    from storycg.shot_contract import EXPRESSION_TAGS
    from storycg.tags import canonical_tag

    shot = entry['shot']
    tag = shot['action_tag']
    canonical = canonical_tag(tag)
    if appearance_forbidden(tag):
        # The appearance rule owns this value: one error here, and the generic appearance pass skips it.
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 带外貌词（比如头发），会改变角色外貌，不允许',
             '换一个不涉及外貌的动作 tag，或删除 shot.action_tag')
        return
    if canonical == canonical_tag(shot['pose']):
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 和 pose 相同，pose 已经表示这个动作',
             '删除 shot.action_tag；或把 pose 改为只表示体位，动作写进 shot.action_tag')
        return
    if canonical in EXPRESSION_TAGS:
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 是表情，表情只放在 shot.expression',
             '删除 shot.action_tag，表情已由 shot.expression 承担；动作改选 Danbooru 上的动作 tag')
        return
    elsewhere = {canonical_tag(t) for t in [*entry['outfit']['tags'], *entry['scene']['tags'],
                                            *shot.get('prop_tags', [])]}
    if canonical in elsewhere:
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 已经出现在这一镜的服装、场景或道具 tag 里',
             '删除 shot.action_tag；该 tag 已经画在别处，动作改选别的动作 tag')
        return
    found = tag_registry.lookup(tag)
    if found is None:
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 在 Danbooru 上不存在',
             '换成 Danbooru 上存在的动作 tag，比如 drinking、reading；或删除 shot.action_tag')
    elif found.get('category') != 0:
        _add(errors, number, 'shot.action_tag', f'动作 tag {tag} 不是 general 类（Danbooru category {found.get("category")}）',
             '换成 general 类的动作 tag，比如 drinking、reading；或删除 shot.action_tag')


def _collect_errors(board, shot_count, identity, config, tag_registry=None):
    """List every failing check on a materialized board. Never mutates the board.

    Mirrors validate_storyboard, validate_contract, validate_story_rules and
    validate_shot_vocab. Each check is listed for each shot it applies to; the
    real validators run afterwards on the success path, so this list is for
    reporting only. tag_registry is needed only when a shot has an action_tag (V2-49).
    """
    from storycg.identity_enrichment import category_for, load_rules
    from storycg.planning import appearance_forbidden, body_target_violation, production_schema, sequence_violation
    from storycg.prompt_rules import companion_conflicts
    from storycg.shot_contract import EXPRESSION_TAGS, moment
    from storycg.shot_vocab import classify_shots, load_vocab, substitute_text
    from storycg.story_rules import StoryRuleViolation, validate_story_rules
    from storycg.tags import canonical_tag
    from storycg.posture_tags import validate_posture_blocks

    errors = []
    shots = board.get('shots', [])

    if len(shots) != shot_count:
        _add(errors, None, 'shots', f'镜数为 {len(shots)}，工单要求 {shot_count}', f'增删镜头，使镜数为 {shot_count}')
    if [entry.get('number') for entry in shots] != list(range(1, len(shots) + 1)):
        _add(errors, None, 'shots[].number', '镜号不是从 1 开始连续编号', '按顺序重新编号为 1, 2, 3, ...')

    for index, entry in enumerate(shots):
        number, shot = index + 1, entry['shot']
        if len(shot.get('prop_tags', [])) + len(shot.get('key_props', [])) > 1:
            _add(errors, number, 'shot.prop_tags', '每镜最多一个道具', '只保留一个背景道具，其余删除')
    for conflict in companion_conflicts(board, config):
        _add(errors, conflict['shot'], 'shot.tags',
             f"tag {conflict['trigger']} 需要 {', '.join(conflict['require'])}，但同镜含有 {', '.join(conflict['conflicts'])}",
             '删除冲突的 tag，或改用需要的 tag')

    try:
        validate_posture_blocks(board)
    except ValueError as exc:
        _add(errors, None, 'shot.tags', f'体位 tag 检查失败：{exc}', '按提示删除或替换体位外的 tag')

    for index, entry in enumerate(shots):
        number, shot = index + 1, entry['shot']
        try:
            moment(shot, f'$.shots[{index}].shot')
        except StoryRuleViolation as exc:
            _add(errors, number, ', '.join(f.split('.shot.')[-1] for f in exc.fields),
                 exc.problem, exc.correction)
        if sequence_violation(shot.get('spatial_relation', '')):
            _add(errors, number, 'shot.spatial_relation',
                 f'这一镜只能画一个时刻，"{shot["spatial_relation"]}" 写了先后两个动作',
                 '只保留画面里的那一刻；前一个动作也要画出来的话，拆成两镜')
        if any(canonical_tag(tag) in EXPRESSION_TAGS for tag in shot.get('prop_tags', [])):
            _add(errors, number, 'shot.prop_tags', '背景道具 tag 不能承担表情', '表情只放在 shot.expression')
        if shot.get('action_tag'):
            if tag_registry is None:
                raise ValueError('collecting shot.action_tag errors needs a tag registry')
            _action_tag_errors(errors, number, entry, tag_registry)

    for error in Draft202012Validator(production_schema()).iter_errors(board):
        number, field = _shot_of_path(error.absolute_path)
        _add(errors, number, field, f'schema: {error.message}', '按 beats-guide 中的 schema 修正该字段')

    if shots:
        first = shots[0]['shot']
        if first['framing'] != 'wide' or first['narrative_role'] != 'establishing':
            _add(errors, 1, 'shot.framing, shot.narrative_role',
                 '首镜必须是 wide 加 establishing', '把第 1 镜改为 framing: wide、narrative_role: establishing')
    for index in range(1, len(shots)):
        number, shot, previous = index + 1, shots[index]['shot'], shots[index - 1]['shot']
        if ((shot['framing'], shot['camera_angle']) == (previous['framing'], previous['camera_angle'])
                and not shot['composition_reason'].strip()):
            _add(errors, number, 'shot.composition_reason',
                 '与上一镜景别和角度相同，需要 composition_reason',
                 '改 framing 或 camera_angle，或填写 composition_reason')
        if ((shot['expression'], shot['action']) == (previous['expression'], previous['action'])
                and not shot.get('unchanged_person_reason', '').strip()):
            _add(errors, number, 'shot.unchanged_person_reason',
                 '与上一镜表情和动作相同，需要 unchanged_person_reason',
                 '改表情或动作，或填写 unchanged_person_reason')

    previous_outfit = None
    for index, entry in enumerate(shots):
        state = entry['outfit']['clothing_state']
        outfit_tags = {canonical_tag(tag) for tag in entry['outfit']['tags']}
        if previous_outfit is not None and state != previous_outfit[0] and outfit_tags == previous_outfit[1]:
            _add(errors, index + 1, 'outfit.clothing_state',
                 f'第 {index + 1} 镜的服装状态变成了 {state}，但服装 tag 没变，请写出 {state} 对应的服装 tag',
                 f'把 {state} 对应的服装 tag 写进 outfit.tags，或保持服装状态与上一镜相同')
        previous_outfit = (state, outfit_tags)
    errors += _new_outfit_colour_errors(shots, identity)

    identity_terms = []
    if identity:
        identity_terms = [identity.get('character_tag'), identity.get('work_tag'), identity.get('input_name')]
    rules = load_rules()
    for index, entry in enumerate(shots):
        number, shot = index + 1, entry['shot']
        # Same clothing filter as validate_storyboard: accessories are exempt from the appearance check.
        outfit = copy.deepcopy(entry['outfit'])
        outfit['tags'] = [tag for tag in outfit['tags']
                          if (category_for(canonical_tag(tag), rules) or {}).get('name') not in ('accessories', 'accessory_color')]
        pairs = [(f'scene.{key}', value, False) for key, field in entry['scene'].items()
                 for value in _strings(field)]
        pairs += [(f'outfit.{key}', value, False) for key, field in outfit.items()
                  for value in _strings(field)]
        pairs += [(f'shot.{key}', value, key in ACTION_FIELDS) for key, field in shot.items()
                  for value in _strings(field)]
        for field, value, body_action in pairs:
            # V2-49: an action_tag, and its copy in shot.tags, is judged once by _action_tag_errors.
            covered = field in ('shot.action_tag', 'shot.tags') and value == shot.get('action_tag')
            if not covered and appearance_forbidden(value, body_action=body_action):
                if body_action and body_target_violation(value) == 'clause':
                    _add(errors, number, field, f'这个字段只能写一个动作子句："{value}"',
                         '只保留一个动作子句（如 "close eyes"、"hands on face"）；原因、时间、并列的第二个动作'
                         '移到别的字段或删去；眼睛状态和体位动作可以保留')
                else:
                    _add(errors, number, field, f'外貌描写 "{value}" 不允许',
                         '删除外貌、发色、瞳色、体型等描写；眼睛状态和体位动作可以保留')
            if any(term and re.search(rf"(?<!\w){re.escape(term.replace('_', ' '))}(?!\w)",
                                      value.replace('_', ' '), re.I) for term in identity_terms):
                _add(errors, number, field, f'角色名或作品名 "{value}" 不允许', '删除角色名、作品名')

    for index, entry in enumerate(shots):
        try:
            validate_story_rules({'shots': [entry]}, allow_obscured=True)
        except StoryRuleViolation as exc:
            fields = ', '.join(exc.fields).replace('$.shots[0]', f'$.shots[{index}]')
            _add(errors, index + 1, fields, exc.problem, exc.correction)
    obscured = sum(1 for entry in shots if _obscured(entry))
    if not (config or {}).get('allow_obscured_protagonist', False) and obscured > 1:
        _add(errors, None, 'shots[].shot.subject_visibility',
             f'遮挡主角或背对镜头的镜头有 {obscured} 个，上限 1',
             '只保留 1 个背影或遮挡镜，其余改为正面或侧面可读')

    vocab = load_vocab(config)
    by_id = {item['id']: item for item in vocab['items']}
    classified = classify_shots(board, vocab=vocab)
    for item in classified:
        if item['grade'] == 'unusable':
            entry = by_id[item['vocab_ids'][0]]
            _add(errors, item['shot'], 'shot.pose, shot.framing, shot.camera_angle',
                 f"动作 {item['action']} 为不可用",
                 f"改用替代组合：{substitute_text(entry)}；或改用稳定组合")
    sometimes = [item for item in classified if item['grade'] == 'sometimes']
    if len(sometimes) > 1:
        for item in sometimes:
            _add(errors, item['shot'], 'shot.pose',
                 f"动作 {item['action']} 为有时{'（表外）' if item['off_table'] else ''}；每组最多 1 镜有时（含表外）",
                 '换用稳定组合；或保留 1 镜有时，其余改为稳定组合')
    return errors


OUTFIT_RULE = '剧情没换衣服时，服装 tag 原样用这几个；换衣服的镜头写新衣服的 tag。'


def collision_notes(config=None):
    """Vocabulary entries that share framing, angle and action (V2-12: expression picks among them)."""
    from storycg.shot_vocab import load_vocab
    groups = {}
    for item in load_vocab(config)['items']:
        for action in item['action_tags']:
            groups.setdefault((item['framing'], item['camera_angle'], action), []).append(item)
    notes = []
    for (framing, angle, action), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        parts = [f"第 {item['id']} 项表情 {'/'.join(item['expression_tags']) or '（无）'}" for item in items]
        notes.append(f"{framing}/{angle}/{action}：" + '；'.join(parts)
                     + '。本镜表情属于其中一部分时按那部分定档；都不属于时取保守档。')
    return notes


def character_guide(identity, config=None):
    """Standard outfit tags and the vocabulary section for this character (V2-12).

    V2-43b: a character outside the WAI list gets only the unsupported line (beats-guide prints that line
    alone and exits 2); the tier lines are gone, the vocabulary has one column.
    """
    from storycg import wai_list
    from storycg.shot_vocab import prompt_section
    tag = identity.get('character_tag')
    unsupported = None
    if tag and not wai_list.tier_row(tag, wai_list.settings_from(config)):
        # V2-38: only characters on the WAI list are supported; the guide says so instead of failing.
        unsupported = '不支持：' + wai_list.refusal_question(wai_list.settings_from(config))
    return {'character': identity.get('character_tag') or identity.get('input_name'),
            'locked': identity.get('status') == 'locked',
            'unsupported': unsupported,
            'outfit_tags': [item['tag'] for item in identity.get('default_outfit_tags', [])],
            'unavailable_tags': [item['tag'] for item in identity.get('unavailable_identity_tags', [])],
            'outfit_rule': OUTFIT_RULE,
            'vocab': prompt_section(config),
            'collision_notes': collision_notes(config)}


def beats_guide(shot_count: int = 4, character_guide_info=None) -> str:
    """Writing guide for the agent: schema, the planner's writing rules, first-shot rule.

    With character_guide_info (from character_guide), also the standard outfit tags and the vocabulary.
    """
    from storycg.planning import FIRST_SHOT_RULE, production_schema, writing_rules
    from storycg.shot_contract import planner_schema
    schema = planner_schema(production_schema())
    return '\n'.join([
        '# 故事分镜（beats）写作说明',
        '',
        f'目标：{shot_count} 镜。输出一个 JSON 对象，符合下面的 schema；不要输出 schema 之外的字段，不要出现派生字段'
        '（action、description、shot.tags 由程序生成）。',
        '',
        '## 1. JSON schema',
        '```json',
        json.dumps(schema, ensure_ascii=False, indent=2),
        '```',
        '',
        '## 2. 写作规则（compile 按这些规则和词汇表校验）',
        writing_rules(shot_count).strip(),
        '',
        '## 3. 首镜规则',
        FIRST_SHOT_RULE.strip(),
        '',
        *_character_section(character_guide_info),
        '## 5. 自查',
        '写好后运行：storycg compile --character <角色> --beats <文件> --shots ' + str(shot_count),
        '有错误按每条的镜号和字段修改，直到通过，再运行 storycg make --beats <文件>。',
        '',
    ])


def _character_section(info):
    if not info:
        return ['## 4. 角色（未指定 --character）', '未指定角色：标准服装与词汇表未列出。'
                '需要时运行 beats-guide --character 角色名。', '']
    lines = ['## 4. 角色：' + str(info['character']) + ('' if info['locked'] else '（身份未锁定，以下信息仅供参考）')]
    if info.get('unsupported'):
        lines.append(info['unsupported'])
    lines += ['标准服装 tag：' + (', '.join(info['outfit_tags']) or '（无）')]
    # V2-51 (D88): tags removed at identity resolution are named, so the writer knows why they are not in the list.
    if info.get('unavailable_tags'):
        lines.append('已去掉 Danbooru 上废弃或不存在的 tag：' + ', '.join(info['unavailable_tags']))
    lines += [info['outfit_rule'],
             info['vocab'].strip(),
             '撞键说明（同一景别、角度、动作有多个条目时）：']
    lines += ['- ' + note for note in info['collision_notes']] or ['- （无）']
    return lines + _action_tag_lines() + ['']


def _action_tag_lines():
    """V2-49: the action tags measured on the base model (config/action_tags.json), printed after the vocabulary."""
    from storycg.shot_contract import load_action_tags
    lines = ['### 动作 tag（shot.action_tag）：基础模型上测过的结果',
             '表外的 tag 也可以用，只要 Danbooru 上存在；没测过，画不画得出不保证。']
    for tag, item in load_action_tags().items():
        note = f"（{item['note']}）" if item.get('note') else ''
        reject = f"【{item['compile']}】" if item.get('compile') else ''
        lines.append(f"- {tag}：画出 {item['drawn']}/{item['total']}{note}{reject}")
    return lines


@contextmanager
def _tag_registry(job_dir, tag_registry, record):
    """The Danbooru tag registry for one compile; record['tag_validation'] keeps only this compile's lookups."""
    if tag_registry is not None:
        before = len(tag_registry.records)
        try:
            yield tag_registry
        finally:
            record['tag_validation'] = tag_registry.records[before:]
        return
    import httpx
    from storycg.tags import TagRegistry
    with httpx.Client(base_url='https://danbooru.donmai.us', timeout=30, trust_env=False) as tag_client:
        registry = TagRegistry(job_dir / 'cache' / 'tags', client=tag_client)
        try:
            yield registry
        finally:
            record['tag_validation'] = registry.records


def compile_beats(job_dir, beats, identity, shot_count, config, *, tag_registry=None):
    """Compile a beats file into storyboard.json without any model call.

    Returns the storyboard. Raises BeatsError listing every error found; the
    compile record (inputs' sha256, status, errors) is appended to plan_log.json.
    """
    from storycg.planning import derive_outfit_changes, production_schema, validate_storyboard
    from storycg.shot_contract import materialize, planner_schema
    from storycg.shot_vocab import validate_shot_vocab
    from storycg.story_rules import validate_story_rules
    from storycg.tags import validate_tag_lists

    job_dir = Path(job_dir)
    beats_path = Path(beats)
    raw = beats_path.read_bytes()
    record = {'planning_contract': 'beats_compiled_v1', 'source_file': str(beats_path),
              'input_sha256': hashlib.sha256(raw).hexdigest(), 'status': 'started'}
    log_path = job_dir / 'plan_log.json'
    log = json.loads(log_path.read_text(encoding='utf-8')) if log_path.exists() else {'calls': []}
    log['calls'].append(record)
    plan_path = job_dir / 'storyboard.json'
    allow_obscured = (config or {}).get('allow_obscured_protagonist', False)
    try:
        if plan_path.exists():
            # Same reuse rule as plan_story: an existing storyboard is re-validated, not recompiled.
            board = json.loads(plan_path.read_text(encoding='utf-8'))
            validate_storyboard(board, shot_count, identity, config)
            validate_shot_vocab(board, config)
            validate_story_rules(board, allow_obscured=allow_obscured)
            record.update(status='reused', note='storyboard.json already exists and was re-validated')
            save_json_atomic(log_path, log)
            return board

        parsed = json.loads(raw)
        schema_errors = list(Draft202012Validator(planner_schema(production_schema())).iter_errors(parsed))
        if schema_errors:
            errors = []
            for error in schema_errors:
                number, field = _shot_of_path(error.absolute_path)
                _add(errors, number, field, f'schema: {error.message}', '按 beats-guide 中的 schema 修正该字段')
            raise BeatsError(errors)
        board = materialize(parsed)
        board, rewrites = rewrite_board(board, config)
        record['rewrites'] = rewrites
        with _tag_registry(job_dir, tag_registry, record) as registry:
            errors = _collect_errors(board, shot_count, identity, config, tag_registry=registry)
            if errors:
                raise BeatsError(errors)

            # The real checks, in plan_story's order. The collector above found nothing,
            # so a failure here is a gap in the collector and is reported as one error.
            validate_storyboard(board, shot_count, identity, config)
            tag_start = len(registry.records)
            validate_tag_lists(board, registry, config, drop_unknown=True)
            dropped = tag_rewrites(registry.records[tag_start:])
        # V2-51 (D88): what validate_tag_lists removed or replaced is reported in the rewrite record, as rewrite_board does.
        for rewrite in dropped:
            _append_note(board, rewrite['text'] + '。')
        record['rewrites'] = record['rewrites'] + dropped
        derive_outfit_changes(board)
        record['outfit_changes'] = [{'shot': entry['number'], 'derived': entry['outfit'].get('change_at_shot')}
                                    for entry in board['shots']]
        board = validate_storyboard(board, shot_count, identity, config)
        record['shot_vocab'] = validate_shot_vocab(board, config)
        validate_story_rules(board, allow_obscured=allow_obscured)
    except BeatsError as exc:
        record.update(status='invalid', errors=exc.errors)
        save_json_atomic(log_path, log)
        raise
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        errors = [{'shot': getattr(exc, 'shot', None), 'field': ', '.join(getattr(exc, 'fields', []) or []) or '-',
                   'problem': f'{type(exc).__name__}: {exc}',
                   'fix': '按该错误信息修改 beats 文件后重新编译'}]
        record.update(status='invalid', errors=errors)
        save_json_atomic(log_path, log)
        raise BeatsError(errors) from exc
    record.update(status='valid')
    save_json_atomic(log_path, log)
    save_json_atomic(plan_path, board)
    return board
