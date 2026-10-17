"""Beats v3 (P0.1, TASK-v2 §14): a thin input layer over the compiled beats format.

v3 names scenes and outfits once and lets each shot refer to them; the compiler expands the references into the
format the rest of the pipeline already reads, so nothing after the compile changes. A file is v3 when it has
`schema_version: 3` or a top-level `scenes`. The older format is still accepted unchanged.

v3 file:
  {"schema_version": 3, "story_note": "",
   "scenes":  {"room": {"tags": [...], "negative_tags": [...], "location": "..", "time": "..", "weather": ".."}},
   "outfits": {"pajamas": {"tags": ["pink_pajamas"], "clothing_state": ".."}},
   "shots":   [{"scene": "room", "outfit": "pajamas", "pose": "sitting", "expression": "smile",
                "framing": "medium", "camera_angle": "front", "action_tag": null, "prop_tags": []}]}
`outfit` omitted or "standard" means the character's locked standard outfit; callers cannot redefine it.
`framing` and `camera_angle` may be left out: they are taken from the stable entries of the shot vocabulary for the
pose. Only gaps are filled; the pose and expression are never replaced. When no stable entry fits, the compile
returns an error that names the missing field and the combinations that exist.
"""
import copy

STANDARD = 'standard'
TOP_KEYS = {'schema_version', 'scenes', 'outfits', 'shots', 'story_note'}
SCENE_KEYS = {'tags', 'negative_tags', 'location', 'time', 'weather', 'lighting'}
OUTFIT_KEYS = {'tags', 'clothing_state'}
TIME_TAGS = ('day', 'night', 'morning', 'afternoon', 'evening', 'noon', 'midnight', 'sunset', 'dusk', 'dawn')
WEATHER_TAGS = ('rain', 'snow', 'snowing', 'fog', 'thunderstorm', 'cloudy_sky', 'overcast', 'wind')


def is_v3(parsed):
    return isinstance(parsed, dict) and (parsed.get('schema_version') == 3 or 'scenes' in parsed)


def _text(name):
    return str(name).replace('_', ' ').strip()


def _stable_combos(items, pose):
    from storycg.shot_vocab import grade_of
    from storycg.beats import _pose_key
    return [i for i in items if grade_of(i) == 'stable' and _pose_key(pose) in [_pose_key(a) for a in i['action_tags']]]


def _combo_text(items):
    return '、'.join(sorted({f"{i['framing']}/{i['camera_angle']}" for i in items}))


def expand(parsed, identity, vocab_items, add_error):
    """Return (expanded beats in the compiled format, notes). Errors go to add_error(shot, field, problem, fix)."""
    notes = []
    for key in sorted(set(parsed) - TOP_KEYS):
        add_error(None, key, f'v3 文件顶层没有这个字段：{key}', f'只写 {", ".join(sorted(TOP_KEYS))}')
    scenes, outfits = parsed.get('scenes') or {}, parsed.get('outfits') or {}
    if not isinstance(scenes, dict) or not scenes:
        add_error(None, 'scenes', 'scenes 必须是对象，至少定义一个场景', '写成 "scenes": {"room": {"tags": ["indoors", "bedroom"]}}')
        scenes = {}
    if not isinstance(outfits, dict):
        add_error(None, 'outfits', 'outfits 必须是对象', '没有换装就整段去掉')
        outfits = {}
    if STANDARD in outfits:
        add_error(None, 'outfits.standard', 'standard 是程序绑定的标准服装名，不能自己定义', '换一个名字')
    for name, scene in scenes.items():
        if not isinstance(scene, dict) or not scene.get('tags'):
            add_error(None, f'scenes.{name}', f'场景 {name} 需要非空的 tags', '写 "tags": ["indoors", "bedroom", "day"]')
        else:
            for key in sorted(set(scene) - SCENE_KEYS):
                add_error(None, f'scenes.{name}.{key}', f'场景里没有这个字段：{key}', f'只写 {", ".join(sorted(SCENE_KEYS))}')
    for name, outfit in outfits.items():
        if not isinstance(outfit, dict) or not outfit.get('tags'):
            add_error(None, f'outfits.{name}', f'服装 {name} 需要非空的 tags', '写带颜色的 tag，如 "tags": ["pink_pajamas"]')
        else:
            for key in sorted(set(outfit) - OUTFIT_KEYS):
                add_error(None, f'outfits.{name}.{key}', f'服装里没有这个字段：{key}', f'只写 {", ".join(sorted(OUTFIT_KEYS))}')
    standard_tags = [item['tag'] for item in (identity or {}).get('default_outfit_tags', [])]

    out_shots, previous = [], None
    for index, raw in enumerate(parsed.get('shots') or []):
        number = index + 1
        if not isinstance(raw, dict):
            add_error(number, 'shots', '每一镜必须是对象', '照例子写')
            continue
        shot = copy.deepcopy(raw)
        scene_name = shot.pop('scene', None)
        if scene_name not in scenes:
            add_error(number, 'scene', f'场景 {scene_name!r} 没有在 scenes 里定义', f'可用：{", ".join(scenes) or "（无）"}')
            scene_def = {'tags': ['indoors']}
        else:
            scene_def = scenes[scene_name]
        outfit_name = shot.pop('outfit', STANDARD) or STANDARD
        if outfit_name == STANDARD:
            if not standard_tags:
                add_error(number, 'outfit', '这个角色没有标准服装 tag', '在 outfits 里定义一套带颜色的服装，并在每镜引用')
            outfit = {'tags': list(standard_tags) or ['shirt'], 'clothing_state': 'standard outfit'}
        elif outfit_name in outfits and isinstance(outfits[outfit_name], dict):
            outfit = {'tags': list(outfits[outfit_name].get('tags') or []),
                      'clothing_state': outfits[outfit_name].get('clothing_state') or _text(outfit_name)}
        else:
            add_error(number, 'outfit', f'服装 {outfit_name!r} 没有在 outfits 里定义', f'可用：standard, {", ".join(outfits)}')
            outfit = {'tags': ['shirt'], 'clothing_state': _text(outfit_name)}
        tags_here = [str(x) for x in (scene_def.get('tags') or [])]
        scene = {'location': scene_def.get('location') or _text(scene_name),
                 'time': scene_def.get('time') or next((x for x in tags_here if x in TIME_TAGS), 'unspecified'),
                 'weather': scene_def.get('weather') or next((x for x in tags_here if x in WEATHER_TAGS), 'clear'),
                 'lighting': scene_def.get('lighting') or 'natural light',
                 'environment_change': 'none' if scene_name == (previous or {}).get('scene') or previous is None
                 else f'moved to {_text(scene_name)}',
                 'tags': list(scene_def.get('tags') or []), 'negative_tags': list(scene_def.get('negative_tags') or [])}

        pose = shot.get('pose')
        if isinstance(pose, str) and ('framing' not in shot or 'camera_angle' not in shot):
            stable = _stable_combos(vocab_items, pose)
            written = {k: shot[k] for k in ('framing', 'camera_angle') if k in shot}
            fits = [i for i in stable if all(i[k] == v for k, v in written.items())]
            missing = [k for k in ('framing', 'camera_angle') if k not in shot]
            if not fits:
                if stable:
                    problem = (f'pose {pose} 没有和已写字段兼容的稳定组合；稳定组合有：{_combo_text(stable)}')
                else:
                    problem = f'pose {pose} 在镜头词汇表里没有稳定组合（表外动作），无法替你补 {" 和 ".join(missing)}'
                add_error(number, ', '.join(f'shot.{k}' for k in missing), problem,
                          f'自己写 {" 和 ".join(missing)}' + (f'，从 {_combo_text(stable)} 里选' if stable else ''))
            else:
                prev_shot = (previous or {}).get('shot') or {}
                # D104 (user): a filled framing is cowboy shot (medium) before wide; wide only when the writer writes it.
                fits.sort(key=lambda i: (i['framing'] == 'wide', i['framing'] == prev_shot.get('framing'),
                                         i['camera_angle'] == prev_shot.get('camera_angle'), i['id']))
                chosen = fits[0]
                for k in missing:
                    shot[k] = chosen[k]
                notes.append({'shot': number, 'rule': 'composition_defaulted', 'field': ', '.join(f'shot.{k}' for k in missing),
                              'from': '省略', 'to': f"{chosen['framing']}/{chosen['camera_angle']}",
                              'reason': f"按词汇表稳定条目 {chosen['id']} 补全",
                              'text': f"第 {number} 镜：{' 和 '.join(missing)} 省略 → {chosen['framing']}/{chosen['camera_angle']}，"
                                      f"按词汇表稳定条目 {chosen['id']} 补全"})
        shot.setdefault('narrative_role', f'shot {number}')
        if isinstance(pose, str):
            shot.setdefault('spatial_relation', _text(pose))
        entry = {'number': number, 'scene': scene, 'outfit': outfit, 'shot': shot}
        out_shots.append(entry)
        previous = {'scene': scene_name, 'shot': shot}
    # Adjacent shots that end up with the same composition or the same person state need a reason; a v3 writer
    # does not invent one, the compiler fills a plain sentence and says so.
    for before, entry in zip(out_shots, out_shots[1:]):
        a, b, number = before['shot'], entry['shot'], entry['number']
        if (a.get('framing'), a.get('camera_angle')) == (b.get('framing'), b.get('camera_angle')) \
                and not str(b.get('composition_reason', '')).strip():
            b['composition_reason'] = 'same framing and angle as the previous shot'
            notes.append({'shot': number, 'rule': 'composition_reason_filled', 'field': 'shot.composition_reason',
                          'from': '空', 'to': '已补说明', 'reason': '和上一镜景别、角度相同',
                          'text': f'第 {number} 镜：景别和角度与上一镜相同，程序补了 composition_reason'})
        if (a.get('expression'), a.get('pose')) == (b.get('expression'), b.get('pose')) \
                and not str(b.get('unchanged_person_reason', '')).strip():
            b['unchanged_person_reason'] = 'the story holds the same person state for this shot'
            notes.append({'shot': number, 'rule': 'unchanged_person_reason_filled', 'field': 'shot.unchanged_person_reason',
                          'from': '空', 'to': '已补说明', 'reason': '表情和动作与上一镜相同',
                          'text': f'第 {number} 镜：表情和动作与上一镜相同，程序补了 unchanged_person_reason'})
    expanded = {'schema_version': 2, 'scope_version': 2, 'story_note': parsed.get('story_note', ''),
                'shots': out_shots}
    return expanded, notes


def beats_v3_schema(shot_count=4):
    """D113: the structural JSON schema of a v3 beats file (what `response_format: json_schema` constrains).

    Structure only: tag existence, vocabulary stability and story rules stay with `compile`. `expression` is the closed
    list of the planner schema; `framing` and `camera_angle` are the closed lists the compiler accepts.
    """
    from storycg.shot_contract import EXPRESSION_TAGS
    tags = {'type': 'array', 'items': {'type': 'string'}}
    scene = {'type': 'object', 'additionalProperties': False, 'required': ['tags'], 'properties': {
        'tags': {**tags, 'minItems': 1}, 'negative_tags': tags, 'location': {'type': 'string'},
        'time': {'type': 'string'}, 'weather': {'type': 'string'}, 'lighting': {'type': 'string'}}}
    outfit = {'type': 'object', 'additionalProperties': False, 'required': ['tags'], 'properties': {
        'tags': {**tags, 'minItems': 1}, 'clothing_state': {'type': 'string'}}}
    shot = {'type': 'object', 'additionalProperties': False, 'required': ['scene', 'pose', 'expression'], 'properties': {
        'scene': {'type': 'string'}, 'outfit': {'type': 'string'}, 'pose': {'type': 'string'},
        'expression': {'enum': list(EXPRESSION_TAGS)},
        'framing': {'enum': ['wide', 'full', 'medium', 'closeup']},
        'camera_angle': {'enum': ['front', 'side', 'above', 'below', 'behind', 'dutch']},
        'action_tag': {'type': 'string'}, 'prop_tags': {**tags, 'maxItems': 1}, 'spatial_relation': {'type': 'string'}}}
    return {'type': 'object', 'additionalProperties': False, 'required': ['schema_version', 'scenes', 'shots'], 'properties': {
        'schema_version': {'enum': [3]}, 'story_note': {'type': 'string'},
        'scenes': {'type': 'object', 'minProperties': 1, 'additionalProperties': scene},
        'outfits': {'type': 'object', 'additionalProperties': outfit},
        'shots': {'type': 'array', 'minItems': shot_count, 'maxItems': shot_count, 'items': shot}}}
