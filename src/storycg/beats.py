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
    shot.update(framing=framing, camera_angle=angle, pose=pose, action=pose, description=pose,
                tags=list(dict.fromkeys([pose, shot['expression']])))


def _append_note(board, sentence):
    note = board.get('story_note', '')
    board['story_note'] = (note + ' ' if note else '') + sentence


def rewrite_board(board, tier, config):
    """Deterministic rewrites before validation (V2-11, TASK-v2 §9). Mutates and returns board.

    Returns (board, rewrites). Each rewrite is logged as "第 N 镜：原值 → 新值，原因" and
    a Chinese sentence is appended to story_note. Rules: off-table pose to the stable
    combo (1), unusable combo to its registered substitute id (2), sometimes-overflow
    convergence from the back (3), indoor precipitation gets a window (5). Clothing (6)
    and the substitute-without-id error are validation errors, see _collect_errors.
    """
    from storycg.prompt_rules import indoor_precipitation
    from storycg.shot_vocab import classify_shots, load_vocab, replacement_for, resolve_grade, vocab_tier
    from storycg.tags import canonical_tag

    tier = vocab_tier(tier, config)
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
                 and resolve_grade(item, tier)[0] == 'stable']
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
            before, combo(shot), '表外组合改用本层级的稳定组合（同一动作）')

    # Rule 2: an unusable combo with a registered substitute id takes that entry's combo.
    for index, entry in enumerate(shots):
        shot = entry['shot']
        hits = _hits(items, shot)
        unusable = [item for item in hits if resolve_grade(item, tier)[0] == 'unusable']
        if not unusable:
            continue
        row = replacement_for(unusable[0], tier)
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
        return [c for c in classify_shots(board, tier, vocab=vocab) if c['grade'] == 'sometimes']

    if len(sometimes_shots()) > 1:
        for index in range(len(shots) - 1, 0, -1):
            if len(sometimes_shots()) <= 1:
                break
            entry = shots[index]
            shot = entry['shot']
            hits = [c for c in classify_shots({'shots': [entry]}, tier, vocab=vocab)[:1]
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


def _collect_errors(board, shot_count, identity, config):
    """List every failing check on a materialized board. Never mutates the board.

    Mirrors validate_storyboard, validate_contract, validate_story_rules and
    validate_shot_vocab. Each check is listed for each shot it applies to; the
    real validators run afterwards on the success path, so this list is for
    reporting only.
    """
    from storycg.identity_enrichment import category_for, load_rules
    from storycg.planning import appearance_forbidden, body_target_violation, production_schema
    from storycg.prompt_rules import companion_conflicts
    from storycg.shot_contract import EXPRESSION_TAGS, moment
    from storycg.shot_vocab import classify_shots, load_vocab, substitute_text, vocab_tier
    from storycg.story_rules import StoryRuleViolation, validate_story_rules
    from storycg.tags import canonical_tag
    from storycg.posture_tags import validate_posture_blocks

    errors = []
    shots = board.get('shots', [])
    tier = vocab_tier((identity or {}).get('identity_tier'), config)

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
        if any(canonical_tag(tag) in EXPRESSION_TAGS for tag in shot.get('prop_tags', [])):
            _add(errors, number, 'shot.prop_tags', '背景道具 tag 不能承担表情', '表情只放在 shot.expression')

    view = {key: value for key, value in board.items() if key not in (
        'possession_swap', 'prop_dependent', 'prop_dependency_reason', 'prop_only_narrative_items')}
    for error in Draft202012Validator(production_schema()).iter_errors(view):
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
            if appearance_forbidden(value, body_action=body_action):
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
    classified = classify_shots(board, tier, vocab=vocab)
    for item in classified:
        if item['grade'] == 'unusable':
            entry = by_id[item['vocab_ids'][0]]
            _add(errors, item['shot'], 'shot.pose, shot.framing, shot.camera_angle',
                 f"动作 {item['action']} 在 {item['grade_basis']} 为不可用",
                 f"改用替代组合：{substitute_text(entry, tier)}；或改用稳定组合")
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


def wai_list_tier(identity, config=None):
    """V2-34: the tier the tier gate gives without a cache entry (V2-25): with character LoRA matching off, a
    character on the WAI list is L2 from its tag alone. Returns (tier, known, source); reads the list only."""
    from storycg import wai_list
    tag = identity.get('character_tag')
    if not tag:
        return None, False, None
    if wai_list.tier_row(tag, wai_list.settings_from(config)):
        return 'L2', True, 'WAI 清单'
    return None, False, None


def character_guide(identity, config=None):
    """Standard outfit tags and the vocabulary section for this character (V2-12).

    The tier comes from the identity once determined, otherwise from the tier cache only.
    Unknown tier: the conservative L1/L2 vocabulary is printed and the guide says so.
    """
    from storycg.shot_vocab import prompt_section
    # V2-34 r1: WAI list first, then the identity already determined. V2-43a: the tier cache is removed.
    tier, known, source = wai_list_tier(identity, config)
    if not known:
        determined = identity.get('identity_tier')
        if determined in ('L1', 'L2'):
            tier, known, source = determined, True, '已判定'
        else:
            tier, known = None, False
    column = 'L2'
    note = (f'层级 {tier}（{source}）' if known else '层级未知')
    note += '；取档按 L2 一列（角色 LoRA 关闭，不读 L1 一列）'
    from storycg import wai_list
    tag = identity.get('character_tag')
    unsupported = None
    if tag and not wai_list.tier_row(tag, wai_list.settings_from(config)):
        # V2-38: only characters on the WAI list are supported; the guide says so instead of failing.
        unsupported = '不支持：' + wai_list.refusal_question(wai_list.settings_from(config))
    return {'character': identity.get('character_tag') or identity.get('input_name'),
            'locked': identity.get('status') == 'locked',
            'unsupported': unsupported,
            'outfit_tags': [item['tag'] for item in identity.get('default_outfit_tags', [])],
            'outfit_rule': OUTFIT_RULE,
            'tier': tier if known else None,
            'tier_known': known,
            'vocab_column': column,
            'tier_note': note,
            'vocab': prompt_section(tier if known else None, config),
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
        return ['## 4. 角色（未指定 --character）', '未指定角色：标准服装与本层级词汇表未列出。'
                '需要时运行 beats-guide --character 角色名。', '']
    lines = ['## 4. 角色：' + str(info['character']) + ('' if info['locked'] else '（身份未锁定，以下信息仅供参考）')]
    if info.get('unsupported'):
        lines.append(info['unsupported'])
    lines += ['标准服装 tag：' + (', '.join(info['outfit_tags']) or '（无）'),
             info['outfit_rule'],
             info['tier_note'],
             info['vocab'].strip(),
             '撞键说明（同一景别、角度、动作有多个条目时）：']
    lines += ['- ' + note for note in info['collision_notes']] or ['- （无）']
    return lines + ['']


def compile_beats(job_dir, beats, identity, shot_count, config, *, tag_registry=None):
    """Compile a beats file into storyboard.json without any model call.

    Returns the storyboard. Raises BeatsError listing every error found; the
    compile record (inputs' sha256, status, errors) is appended to plan_log.json.
    """
    from storycg.planning import derive_outfit_changes, production_schema, validate_storyboard
    from storycg.shot_contract import materialize, planner_schema
    from storycg.shot_vocab import validate_shot_vocab
    from storycg.story_rules import validate_story_rules
    from storycg.tags import TagRegistry, validate_tag_lists

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
            validate_shot_vocab(board, identity.get('identity_tier'), config)
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
        board, rewrites = rewrite_board(board, identity.get('identity_tier'), config)
        record['rewrites'] = rewrites
        errors = _collect_errors(board, shot_count, identity, config)
        if errors:
            raise BeatsError(errors)

        # The real checks, in plan_story's order. The collector above found nothing,
        # so a failure here is a gap in the collector and is reported as one error.
        validate_storyboard(board, shot_count, identity, config)
        if tag_registry is None:
            import httpx
            with httpx.Client(base_url='https://danbooru.donmai.us', timeout=30, trust_env=False) as tag_client:
                registry = TagRegistry(job_dir / 'cache' / 'tags', client=tag_client)
                try:
                    validate_tag_lists(board, registry, config, drop_unknown=True)
                finally:
                    record['tag_validation'] = registry.records
        else:
            before = len(tag_registry.records)
            try:
                validate_tag_lists(board, tag_registry, config, drop_unknown=True)
            finally:
                record['tag_validation'] = tag_registry.records[before:]
        derive_outfit_changes(board)
        record['outfit_changes'] = [{'shot': entry['number'], 'derived': entry['outfit'].get('change_at_shot')}
                                    for entry in board['shots']]
        board = validate_storyboard(board, shot_count, identity, config)
        record['shot_vocab'] = validate_shot_vocab(board, identity.get('identity_tier'), config)
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
