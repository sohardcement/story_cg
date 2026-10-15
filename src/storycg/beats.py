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
# V2-62 (D96, TASK-v2 §12 item 5): the fields that enter the image prompt. Only these get the appearance check;
# the display fields (prose such as composition_reason, spatial_relation, clothing_state, scene.location) are
# checked for character and work names only.
PROMPT_FIELDS = frozenset({'scene.tags', 'scene.negative_tags', 'outfit.tags', 'shot.pose', 'shot.action',
                           'shot.description', 'shot.tags', 'shot.action_tag', 'shot.expression', 'shot.prop_tags'})


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
        """Stable entries for this pose. Order: framing, angle, id. (D98: the first shot is no longer wide-only.)"""
        found = [item for item in items if _pose_key(pose) in
                 [_pose_key(a) for a in item['action_tags']]
                 and grade_of(item) == 'stable']
        return sorted(found, key=lambda item: (item['framing'] != shot['framing'],
                                               item['camera_angle'] != shot['camera_angle'], item['id']))

    shots = board['shots']
    # D98 (user): the first shot is not forced to wide or establishing any more; it is planned like every other shot.
    # Rule 0b (V2-62): an outfit is decided by its tags. A shot with the previous shot's outfit tags keeps the
    # previous clothing_state text, so the review checklist matches what is drawn.
    from storycg.tags import canonical_tag as _canonical
    for previous, entry in zip(shots, shots[1:]):
        same_tags = {_canonical(t) for t in previous['outfit']['tags']} == {_canonical(t) for t in entry['outfit']['tags']}
        if same_tags and entry['outfit']['clothing_state'] != previous['outfit']['clothing_state']:
            log(entry['number'], 'clothing_state_aligned', 'outfit.clothing_state',
                repr(entry['outfit']['clothing_state']), repr(previous['outfit']['clothing_state']),
                '服装 tag 和上一镜相同，服装状态沿用上一镜；要画出变化，请写出对应的服装 tag')
            entry['outfit']['clothing_state'] = previous['outfit']['clothing_state']
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

    # Rule 4 (V2-66): a rewrite can make a shot's framing and angle equal to the previous shot's; the writer could
    # not know, so the code fills the composition_reason (display text) and logs it.
    touched = {r['shot'] for r in rewrites if r['rule'] in ('off_table_stable', 'unusable_substitute', 'sometimes_overflow')}
    for previous, entry in zip(shots, shots[1:]):
        a, b = previous['shot'], entry['shot']
        if ((a['framing'], a['camera_angle']) == (b['framing'], b['camera_angle']) and not b['composition_reason'].strip()
                and {previous['number'], entry['number']} & touched):
            b['composition_reason'] = 'same framing and angle as the previous shot after an automatic rewrite'
            log(entry['number'], 'composition_reason_filled', 'shot.composition_reason', '空', '已补说明',
                '改写后和上一镜景别、角度相同，程序补了 composition_reason；想换机位请改 framing 或 camera_angle')
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


def _new_outfit_colour_errors(shots, identity, config=None):
    """V2-47: a segment of shots in a new outfit must name a colour in one of its outfit tags.

    Segments split where the canonical outfit tag set changes (the same rule as change_at_shot). A segment's new tags
    are its tags that are not in the standard outfit and not in NOT_NEW_OUTFIT_TAGS; a segment with no new tag, or a
    character with no standard outfit, is not checked. Reported once per segment, on its first shot.
    """
    from storycg.tags import canonical_tag, color_split
    standard = {canonical_tag(item['tag']) for item in (identity or {}).get('default_outfit_tags', [])}
    exempt = set(NOT_NEW_OUTFIT_TAGS)
    if not standard:
        return []
    segments = []
    for index, entry in enumerate(shots):
        tags = [canonical_tag(tag) for tag in entry['outfit']['tags']]
        if not segments or set(tags) != set(segments[-1][1]):
            segments.append((index + 1, tags))
    errors = []
    for number, tags in segments:
        new = list(dict.fromkeys(tag for tag in tags if tag not in standard and tag not in exempt))
        if new and not any(color_split(tag) for tag in new):
            _add(errors, number, 'outfit.tags',
                 f'第 {number} 镜换上的新衣服没有写颜色（{", ".join(new)}）：只写款式时模型容易画回角色自己的衣服，前后镜的颜色也会变',
                 '给主要的新衣服写一个带颜色、并且 Danbooru 上存在的 tag，比如 brown_coat、pink_pajamas，颜色最好和标准服装不同；同一套衣服的各镜用同一组 tag')
    return errors


def _drop_missing_prop_tags(board, records):
    """V2-55 reported a scene tag that validate_tag_lists removed while the shot still named it as its prop.
    V2-63 (D96): the code removes it from prop_tags too and returns rewrite records; the object is not drawn."""
    rewrites, seen = [], set()
    for record in records:
        match = re.match(r'^(\d+)\.scene\.tags$', record.get('field') or '')
        if record.get('action') != 'discarded' or not match:
            continue
        number, tag = int(match.group(1)), record['input']
        entry = next(e for e in board['shots'] if e['number'] == number)
        props = entry['shot'].get('prop_tags', [])
        if tag in props and (number, tag) not in seen:
            seen.add((number, tag))
            entry['shot']['prop_tags'] = [p for p in props if p != tag]
            text = f'第 {number} 镜：shot.prop_tags 的 {tag} → 删除，Danbooru 上不存在或已废弃，这一镜不会画出它'
            rewrites.append({'shot': number, 'rule': 'prop_tag_unavailable', 'field': 'shot.prop_tags', 'from': tag,
                             'to': None, 'reason': 'Danbooru 上不存在或已废弃', 'text': text})
    return rewrites


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
    from storycg.planning import (appearance_forbidden, body_action_verbs, body_part_words, body_target_violation,
                                  production_schema, sequence_violation)
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

    # V2-62 (D96): a clothing_state change without an outfit tag change is no longer an error; rewrite_board
    # aligns the text to the previous shot (rule 0b) and logs it.
    errors += _new_outfit_colour_errors(shots, identity, config)

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
            # V2-62: action, description and the pose's copy in tags are derived from pose; report it once, on pose.
            covered = covered or field in ('shot.action', 'shot.description') or (field == 'shot.tags' and value == shot.get('pose'))
            if not covered and field in PROMPT_FIELDS and appearance_forbidden(value, body_action=body_action):
                if body_action and body_target_violation(value) == 'clause':
                    parts = '、'.join(body_part_words(value))
                    verbs = '、'.join(body_action_verbs())
                    _add(errors, number, field, f'这个字段里有身体部位词（{parts}），但整句不是认可的写法："{value}"',
                         '只写两种认可的写法之一：① 动词 + [the/own/their/her/his] + 部位，如 "touching lips"，'
                         f'动词取自 ACTION_VERBS：{verbs}（也可用它们的 -ing、-ed 等变形）；'
                         '② hands on/over/against + 部位，如 "hands on face"。'
                         '两种写法前面都可以加主语 subject / person / character / hands / fingers / palms，'
                         '或 move to / reach to；整句只能是这一种写法，前后不能再加别的词（"spoon touching lips" 不行）。'
                         '也可以去掉身体部位词，比如 "tasting soup"')
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
        for item in sometimes:  # V2-62 (D96): off-table shots are 'untested' and take no part in this quota
            _add(errors, item['shot'], 'shot.pose',
                 f"动作 {item['action']} 为有时；每组最多 1 镜有时（表外动作不算）",
                 '换用稳定组合；或保留 1 镜有时，其余改为稳定组合')
    return errors


OUTFIT_RULE = '剧情没换衣服时，服装 tag 原样用这几个；换衣服的镜头写新衣服的 tag。标准服装是兔女郎装、泳装、战斗服这类场合服装，又和剧情场景不合时（比如穿兔女郎装去海边散步），可以从第 1 镜起整组换成一套合场景的日常服装：主要衣服写带颜色的 tag（如 white_dress），每镜的服装 tag 和 clothing_state 保持一致。'


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
        notes.append(f"{framing}/{angle}/{action}：" + '；'.join(parts) + '，按表情定档，都不属于时取保守档')
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
    if tag and unsupported is None:
        unsupported = '不支持：' + question if question else None
    return {'character': identity.get('character_tag') or identity.get('input_name'),
            'locked': identity.get('status') == 'locked',
            'unsupported': unsupported,
            'outfit_tags': [item['tag'] for item in identity.get('default_outfit_tags', [])],
            'unavailable_tags': [item['tag'] for item in identity.get('unavailable_identity_tags', [])],
            'outfit_rule': OUTFIT_RULE,
            'vocab': prompt_section(config),
            'collision_notes': collision_notes(config)}


# V2-63 (D96, TASK-v2 §12 item 1): boilerplate fields the writer may leave out; compile fills them before the schema
# check. Only fields with one obvious value are here; everything that tells the story stays required.
SHOT_DEFAULTS = {'action_tag': None, 'prop_tags': [], 'change_from_previous': 'none', 'composition_reason': '',
                 'unchanged_person_reason': '', 'subject_visibility': 'clear', 'other_people': [],
                 'hand_owners': ['subject'], 'reflection': False, 'protagonist_instances': 1}
SCENE_DEFAULTS = {'environment_change': 'none', 'lighting': 'natural light', 'negative_tags': []}
TOP_DEFAULTS = {'schema_version': 2, 'scope_version': 2, 'story_note': ''}


def fill_defaults(parsed):
    """Fill the omitted boilerplate fields in place; return the names filled (field paths, deduplicated)."""
    import copy as _copy
    filled = []
    if not isinstance(parsed, dict):
        return filled
    for key, value in TOP_DEFAULTS.items():
        if key not in parsed:
            parsed[key] = value
            filled.append(key)
    for entry in parsed.get('shots') or []:
        if not isinstance(entry, dict):
            continue
        for group, defaults in (('shot', SHOT_DEFAULTS), ('scene', SCENE_DEFAULTS)):
            block = entry.get(group)
            if not isinstance(block, dict):
                continue
            for key, value in defaults.items():
                if key not in block:
                    block[key] = _copy.deepcopy(value)
                    filled.append(f'{group}.{key}')
    return list(dict.fromkeys(filled))


GUIDE_EXAMPLE = """{"schema_version": 3,
 "scenes": {"room": {"tags": ["indoors", "bedroom", "day"]},
            "street": {"tags": ["outdoors", "street", "day"]}},
 "outfits": {"coat": {"tags": ["brown_coat"]}},
 "shots": [
  {"scene": "room", "pose": "sitting", "expression": "expressionless"},
  {"scene": "room", "pose": "standing", "expression": "surprised"},
  {"scene": "street", "outfit": "coat", "pose": "running", "expression": "worried", "prop_tags": ["umbrella"]},
  {"scene": "street", "outfit": "coat", "pose": "walking", "expression": "light_smile", "framing": "wide", "camera_angle": "side"}
 ]}"""


def beats_guide(shot_count: int = 4, character_guide_info=None) -> str:
    """P0.1 (beats v3): the writing guide. Scenes and outfits are named once; each shot only says what changes.

    With character_guide_info (from character_guide), also the standard outfit tags, the vocabulary and the action tags.
    """
    from storycg.shot_contract import EXPRESSION_TAGS
    lines = [
        f'# 故事分镜（beats）写作说明：{shot_count} 镜',
        '',
        f'写一个 JSON 对象，照下面的例子写 {shot_count} 镜。场景和服装各写一次、起名字，每一镜只写引用和变化的东西。',
        '',
        '## 1. 例子',
        '```json', GUIDE_EXAMPLE, '```',
        '- scenes：每个场景写一次 tags（地点、时间、天气、光线）。场景变了就用另一个名字。',
        '- outfits：只在换衣服时写，每套写带颜色的 tag。镜里不写 outfit，就是角色的标准服装。',
        '- 每镜必须有 scene、pose、expression。其余都可以省略：framing 和 camera_angle 省略时，程序按词汇表里这个 pose 的'
        '稳定组合补上，并在 rewrites 里告诉你补了什么；找不到稳定组合时报错，那时自己写。',
        '- 可选：framing、camera_angle、action_tag、prop_tags、spatial_relation（给人看的一句英文）、顶层 story_note。'
        '相邻镜头景别角度相同、表情 pose 相同的说明，程序会补。',
        '',
        '## 2. 只有 tag 会画进图',
        '- 进提示词的只有 scenes 和 outfits 里的 tags、pose、action_tag、expression、prop_tags。'
        '只写 Danbooru 上存在的 tag（小写、下划线）。compile 输出的 prompt_preview 就是最终提示词。',
        '- 任何 tag 里都不写外貌（发色、瞳色、发型、肤色、体型），身份 tag 由程序加。',
        '- 一镜只画一个瞬间。先后两个动作拆成两镜。',
        '',
        '## 3. 字段',
        '- framing：wide 远景、full 全身、medium 中景、closeup 近景。camera_angle：front、side、above、below、behind、dutch。',
        '- pose：剧情里的动作有对应的 Danbooru tag 就直接用，词汇表里没有也可以，不要换成意思相近的别的动作。'
        '第 5 节词汇表只说明哪些组合画得稳：表里有的，省略 framing 和 camera_angle 时程序能补；表外的要自己写。',
        '- 剧情点名的物件和它的颜色，要写进 scene.tags 或 prop_tags，别省略。',
        '- action_tag：pose 之外还要画的一个动作，比如 drinking、reading、waving。借用意思不同的 tag，'
        '就会画成那个 tag 本来的意思。',
        '- expression 只能取：' + '、'.join(EXPRESSION_TAGS) + '。睡着、闭眼、哭这类状态写在这里。',
        '- prop_tags：这一镜要画出来的物件或动物，最多 1 个。只写在说明文字里的东西画不出来。',
        '- 场景物件上限：近景 0 个（道具除外），中景 2 个，全身和远景 3 个。电脑、书、杯子、手机这类会引出动作的物件，'
        '只在它是这一镜的道具时才写。室内写了下雨，程序会补窗户；睡着类 pose，程序会补闭眼。',
        '- 场景的 negative_tags：不该出现的天气或元素，比如雨停了就写 rain。',
        '- 背影或遮挡主角的镜头最多 1 个。',
        '- 剧情靠拿错东西、认错人推进时，改成用表情和动作表达，并在 story_note 里用一句中文说明。',
        '',
        *_character_section(character_guide_info),
        '## 7. 自查',
        f'运行 storycg compile --character <角色> --beats <文件> --shots {shot_count}。'
        'invalid 时按每条错误的镜号和字段改，再编译。valid 后看 rewrites 和 prompt_preview，'
        '确认要画的 tag 都在，再运行 storycg make --beats <文件>。',
        '',
    ]
    return '\n'.join(lines)


def _character_section(info):
    if not info:
        return ['## 4–6. 角色（未指定 --character）',
                '未指定角色，标准服装、词汇表和动作 tag 没有列出。需要时运行 beats-guide --character 角色名。', '']
    lines = ['## 4. 服装：' + str(info['character']) + ('' if info['locked'] else '（身份未锁定，以下仅供参考）')]
    if info.get('unsupported'):
        lines.append(info['unsupported'])
    lines.append('- 标准服装 tag：' + (', '.join(info['outfit_tags']) or '（无）'))
    # V2-51 (D88): tags removed at identity resolution are named, so the writer knows why they are not in the list.
    if info.get('unavailable_tags'):
        lines.append('- 已去掉 Danbooru 上废弃或不存在的 tag：' + ', '.join(info['unavailable_tags']))
    lines += ['- ' + info['outfit_rule'],
              '- 换没换衣服只看 outfit.tags。主要的新衣服写带颜色的 tag（pink_pajamas、brown_coat）；同一套衣服每镜 tag 相同。'
              'clothing_state 只是说明，tag 没变时程序沿用上一镜的写法。',
              '', '## 5. 词汇表', info['vocab'].strip()]
    if info['collision_notes']:
        lines.append('- 撞键说明（同一景别、角度、pose 有多项）：' + '。'.join(info['collision_notes']) + '。')
    return lines + [''] + _action_tag_lines() + ['']


def _action_tag_lines():
    """V2-49: the action tags measured on the base model (config/action_tags.json), compact (V2-63)."""
    from storycg.shot_contract import load_action_tags
    table = load_action_tags()
    good = [f"{tag} {item['drawn']}/{item['total']}" for tag, item in table.items()
            if not item.get('compile') and not item.get('note') and item['drawn'] * 2 > item['total']]
    weak = [f"{tag} {item['drawn']}/{item['total']}" + (f"（{item['note']}）" if item.get('note') else '')
            for tag, item in table.items() if not item.get('compile') and (item.get('note') or item['drawn'] * 2 <= item['total'])]
    refused = [f"{tag}（{item['compile']}）" for tag, item in table.items() if item.get('compile')]
    lines = ['## 6. 动作 tag（shot.action_tag）：基础模型上测过的结果',
             '- 画得出：' + '、'.join(good)]
    if weak:
        lines.append('- 不建议：' + '、'.join(weak))
    if refused:
        lines.append('- compile 不接受：' + '、'.join(refused))
    lines.append('- 表外的 tag 也可以用，只要 Danbooru 上存在；没测过，画不画得出不保证。')
    return lines


def prompt_preview(board, identity, config):
    """V2-62 (D96, TASK-v2 §12 item 7): each shot's final positive and negative prompt, built the way the candidates
    are submitted (workflow.build_workflow, assembly.full_negative_prompt). No model call, no image."""
    from storycg.assembly import assemble_prompt, full_negative_prompt
    rows = []
    for entry in board['shots']:
        try:
            parts = assemble_prompt(identity, entry, config=config,
                                    **({'props': board['props']} if board.get('props') else {}))
        except (KeyError, ValueError) as exc:  # a preview never fails a valid compile
            rows.append({'shot': entry['number'], 'error': f'{type(exc).__name__}: {exc}'})
            continue
        rows.append({'shot': entry['number'], 'positive': f"{config['positive_prefix']}, {parts['positive_tags']}",
                     'negative': full_negative_prompt(config, parts),
                     'dropped_scene_tags': list(parts.get('dropped_scene_tags') or [])})
    return rows


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
        # V2-64 (D96): one project-level cache for every compile (it was a new folder per compile), 30 days.
        job_cache = job_dir / 'cache' / 'tags'  # a job that already carries its own cache keeps using it
        project_cache = Path(__file__).resolve().parents[2] / 'cache' / 'tags'
        registry = TagRegistry(job_cache if job_cache.is_dir() and any(job_cache.iterdir()) else project_cache,
                               client=tag_client, max_age_days=30)
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
    from storycg.shot_vocab import load_vocab, validate_shot_vocab
    from storycg.story_rules import validate_story_rules
    from storycg.tags import validate_tag_lists
    from storycg.beats_v3 import expand as expand_v3, is_v3

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
        if isinstance(parsed, dict) and '$schema' in parsed:
            # V2-62 (D96): a copied schema header is harmless; drop it instead of failing the whole file.
            parsed.pop('$schema')
            record['dropped_keys'] = ['$schema']
        v3_notes = []
        if is_v3(parsed):
            # P0.1: scenes and outfits are named once and expanded here; the pipeline after this reads the old format.
            v3_errors = []
            parsed, v3_notes = expand_v3(parsed, identity, load_vocab(config)['items'],
                                         lambda shot, field, problem, fix: _add(v3_errors, shot, field, problem, fix))
            record['expanded_from'] = 'beats_v3'
            if v3_errors:
                raise BeatsError(v3_errors)
        record['defaults_filled'] = fill_defaults(parsed)  # V2-63 (D96): omitted boilerplate gets its one value
        schema_errors = list(Draft202012Validator(planner_schema(production_schema())).iter_errors(parsed))
        if schema_errors:
            errors = []
            for error in schema_errors:
                number, field = _shot_of_path(error.absolute_path)
                _add(errors, number, field, f'schema: {error.message}', '按 beats-guide 中的 schema 修正该字段')
            raise BeatsError(errors)
        board = materialize(parsed)
        for note in v3_notes:
            _append_note(board, note['text'] + '。')
        board, rewrites = rewrite_board(board, config)
        rewrites = [*v3_notes, *rewrites]
        if record.get('dropped_keys'):
            rewrites = [{'shot': None, 'rule': 'dropped_key', 'field': '$schema', 'from': '$schema', 'to': None,
                         'reason': 'beats 文件不需要 schema 头', 'text': '顶层的 $schema 字段 → 删除，beats 文件不需要 schema 头'},
                        *rewrites]
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
            # V2-63 (D96): a removed tag that the shot still names as its prop is dropped from prop_tags as well.
            dropped = _drop_missing_prop_tags(board, registry.records[tag_start:]) + tag_rewrites(registry.records[tag_start:])
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
