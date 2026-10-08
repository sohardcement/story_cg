"""Schema-bound runtime storyboarding with appearance exclusion."""

import json
import copy
import re
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator

from storycg.state import save_json_atomic


TEXT = {"type": "string", "minLength": 1}
TAGS = {"type": "array", "items": TEXT, "uniqueItems": True}
SCENE = {"type": "object", "additionalProperties": False,
         "required": ["location", "time", "weather", "lighting", "environment_change", "tags", "negative_tags"],
         "properties": {**{key: TEXT for key in
                        ["location", "time", "weather", "lighting", "environment_change"]},
                        "tags": {**TAGS, "minItems": 1}, "negative_tags": TAGS}}
OUTFIT = {"type": "object", "additionalProperties": False,
          "required": ["clothing_state", "change_at_shot", "tags"],
          "properties": {"clothing_state": TEXT, "tags": {**TAGS, "minItems": 1},
                         "change_at_shot": {"type": ["integer", "null"], "minimum": 1}}}
SHOT = {"type": "object", "additionalProperties": False,
        "required": ["narrative_role", "description", "action", "expression", "key_props", "tags",
                     "spatial_relation", "framing", "camera_angle", "change_from_previous", "composition_reason"],
        "properties": {**{key: TEXT for key in ["narrative_role", "description", "action", "expression",
                                                "spatial_relation", "change_from_previous"]},
                       "composition_reason": {"type": "string"},
                       "tags": {**TAGS, "minItems": 1},
                       # Opaque archived records; new planning never writes bindings.
                       "key_props": {"type": "array", "items": {"type": "object"}},
                       "prop_tags": {**TAGS, "maxItems": 1},
                       "camera_angle": {"enum": ["front", "above", "below", "side", "behind", "dutch"]},
                       "framing": {"enum": ["wide", "full", "medium", "closeup"]}}}
STORYBOARD_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                     "type": "object", "additionalProperties": False,
                     "required": ["props", "shots"], "properties": {
                         "props": {"type": "array", "items": {"type": "object"}},
                         "shots": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False,
                         "required": ["number", "scene", "outfit", "shot"],
                         "properties": {"number": {"type": "integer", "minimum": 1},
                                        "scene": SCENE, "outfit": OUTFIT, "shot": SHOT}}}}}

# Optional on legacy boards; production planning requires explicit person visibility.
SHOT['properties'].update({
    'subject_visibility':{'enum':['clear','obscured']},
    'reflection':{'type':'boolean','description':'True only when the shot explicitly includes one mirror or glass reflection of the protagonist.'},
    'other_people':{'type':'array','items':{'enum':['passerby','back_view','offscreen','partial']}},
    'hand_owners':{'type':'array','items':{'enum':['subject','anonymous_other']}}})


from storycg.shot_contract import extend_schema, validate_contract, RULE_PROMPT as CONTRACT_PROMPT
extend_schema(STORYBOARD_SCHEMA)


def production_schema():
    schema=copy.deepcopy(STORYBOARD_SCHEMA)
    shot=schema['properties']['shots']['items']['properties']['shot']
    shot['required'] += ['subject_visibility','other_people','hand_owners']
    return extend_schema(schema, required=True)


from functools import lru_cache

# Body-action verb stems with their inflection groups; BODY_ACTION and the
# eye-state exemption share this one table.
ACTION_VERBS = (
    ('cover', '(?:s|ed|ing)?'), ('shield', '(?:s|ed|ing)?'), ('touch', '(?:es|ed|ing)?'),
    ('rub', '(?:s|bed|bing)?'), ('press', '(?:es|ed|ing)?'), ('hold', '(?:s|ing)?'),
    ('rais', '(?:e|es|ed|ing)'), ('lower', '(?:s|ed|ing)?'), ('tilt', '(?:s|ed|ing)?'),
    ('turn', '(?:s|ed|ing)?'), ('move', '(?:s|d|ing)?'), ('reach', '(?:es|ed|ing)'),
    ('wipe', '(?:s|d|ing)'), ('close', '(?:s|d|ing)'),
)

APPEARANCE_EN = re.compile(
    r"\b(?:hair|haired|hairstyle|bangs|twintails?|ponytails?|braids?|eyes?|eyed|iris|pupils?|"
    r"eyelashes|eyebrows?|ears?|skin|complexion|face|facial features|body type|"
    r"breasts?|bust|slender|muscular|plump|chubby|thin|fat|ahoge|height|stature|build|figure|petite|elf|horns?|tails?|fangs?|freckles|moles?|"
    r"lips?|nose|mouth)\b", re.I)
APPEARANCE_ZH = re.compile(
    r"头发|发色|发型|发丝|长发|短发|卷发|直发|银发|金发|黑发|白发|红发|蓝发|绿发|紫发|粉发|"
    r"刘海|马尾|辫子|瞳色|瞳孔|眼睛|蓝眸|绿眸|红眸|眉毛|耳朵|肤色|皮肤|脸型|面容|体型|身高|精灵耳|尾巴|角"
)

# A body part can be an action target without describing its appearance.
# Match the direct object only; attributes such as color/shape still hit the
# unchanged appearance filter, as do all mentions in scene/outfit/props.
BODY_ACTION = re.compile(
    r'\b(?:' + '|'.join(stem + group for stem, group in ACTION_VERBS) + r')'
    r'\s+(?:(?:the|own|their|her|his)\s+)?(?:face|eyes?|mouth|ears?|lips?|nose)\b|'
    r'\b(?:hands?|fingers?|palms?)\s+(?:on|over|against)\s+'
    r'(?:(?:the|own|their|her|his)\s+)?(?:face|eyes?|mouth|ears?|lips?|nose)\b', re.I)

# Eye states and actions on the eyes are not appearance (V2-08): a value whose
# only appearance words are the eye nouns passes when every content word of the
# eye-mention segment belongs to the state/action vocabulary below. Color,
# shape and size words (blue, tareme, big, ...) are not in it, so they still
# reject. Chinese appearance words are not exempted.
EYE_NOUN = re.compile(r'\bey(?:e|es)\b', re.I)
FUNCTION_WORDS = frozenset(
    'a an the own her his their its with and or is are was were be being while '
    'from at of in on to as both into'.split())


def _inflect(stem, suffixes):
    forms = {stem}
    for suffix in suffixes:
        if suffix == 'ing' and stem.endswith('e'):
            forms.add(stem[:-1] + suffix)
        else:
            forms.add(stem + suffix)
    if len(stem) >= 3 and stem[-1] not in 'aeiou' and stem[-2] in 'aeiou' and stem[-3] not in 'aeiou':
        forms |= {stem + stem[-1] + 'ing', stem + stem[-1] + 'ed'}
    return forms


def _action_verb_forms():
    forms = set()
    for stem, group in ACTION_VERBS:
        forms |= _inflect(stem, re.findall(r'[a-z]+', group))
    return forms


@lru_cache(maxsize=1)
def _eye_exemption_words():
    from storycg.eye_state import eye_state_words
    from storycg.shot_contract import EXPRESSION_TAGS
    words = (set(eye_state_words()) | FUNCTION_WORDS | _action_verb_forms()
             | {'eye', 'eyes', 'hand', 'hands', 'finger', 'fingers', 'palm', 'palms',
                'subject', 'person', 'character', 'she', 'he', 'they'})
    expression_words = set()
    for tag in EXPRESSION_TAGS:
        expression_words |= {word for word in re.split(r'[^a-z0-9]+', tag) if word}
    try:
        from storycg.shot_vocab import load_vocab
        for item in load_vocab()['items']:
            for tag in item.get('expression_tags', []):
                expression_words |= {word for word in re.split(r'[^a-z0-9]+', tag.lower()) if word}
    except (OSError, ValueError, KeyError):
        pass  # Additive guidance only; the snapshot words above still apply.
    for word in expression_words:
        words |= _inflect(word, ('s', 'ing'))
    return frozenset(words)


def _eyes_state_or_action_only(normalized):
    """True when the only appearance words are eye nouns in state/action use."""
    remainder = EYE_NOUN.sub('', normalized)
    if APPEARANCE_EN.search(remainder):
        return False
    allowed = _eye_exemption_words()
    for segment in re.split(r'[,;.]\s*', normalized):
        if not EYE_NOUN.search(segment):
            continue
        words = {word.lower() for word in re.split(r'[^A-Za-z0-9]+', segment) if word}
        if not words <= allowed:
            return False
    return True


def appearance_forbidden(value, *, body_action=False):
    normalized = re.sub(r'[_-]', ' ', value)
    normalized = re.sub(r'\beye[ -]level\b', '', normalized, flags=re.I)
    if body_action:
        # Exempt a complete plain action, never remove a body noun from a
        # larger sentence: trailing/elliptical attributes would then escape the
        # unchanged appearance filter. Unsupported compound prose is rejected.
        plain_action = re.fullmatch(
            r'(?:(?:subject|person|character|hands?|fingers?|palms?)\s+)?'
            r'(?:(?:move(?:s|d|ing)?|reach(?:es|ed|ing)?)\s+to\s+)?'
            r'(?:' + BODY_ACTION.pattern + r')[.!]?', normalized.strip(), re.I)
        if plain_action:
            normalized = ''
    en_hit = APPEARANCE_EN.search(normalized)
    zh_hit = APPEARANCE_ZH.search(value)
    if en_hit and not zh_hit and _eyes_state_or_action_only(normalized):
        return False
    return bool(en_hit or zh_hit)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
def derive_outfit_changes(board: dict) -> dict:
    """Set outfit.change_at_shot from outfit tag sets (V2-08), in place.

    Shot 1 is always null; any later shot whose outfit tag set differs from the
    previous shot's gets its own shot number, otherwise null. Model-supplied
    values are overwritten, so change_at_shot is never a rejection reason.
    """
    from storycg.tags import canonical_tag
    previous = None
    for entry in board.get('shots', []):
        outfit = entry.setdefault('outfit', {})
        tags = {canonical_tag(tag) for tag in outfit.get('tags', [])}
        outfit['change_at_shot'] = entry.get('number') if (previous is not None and tags != previous) else None
        previous = tags
    return board


def validate_storyboard(board: dict, shot_count: int, identity: dict | None = None,
                        config: dict | None = None) -> dict:
    for entry in board.get('shots', []):
        shot = entry['shot']
        if len(shot.get('prop_tags', [])) + len(shot.get('key_props', [])) > 1:
            raise ValueError('each shot permits at most one prop (key prop or background tag)')
    from storycg.prompt_rules import companion_conflicts
    conflicts = companion_conflicts(board, config)
    if conflicts:
        detail = '; '.join(f"shot {c['shot']} tag {c['trigger']} requires {', '.join(c['require'])} but has {', '.join(c['conflicts'])}"
                           for c in conflicts)
        raise ValueError('action companion tag conflict: ' + detail)
    validate_contract(board)
    view = {key: value for key, value in board.items() if key not in (
        'possession_swap', 'prop_dependent', 'prop_dependency_reason', 'prop_only_narrative_items')}
    errors = list(Draft202012Validator(production_schema() if board.get("schema_version")==2 else STORYBOARD_SCHEMA).iter_errors(view))
    if errors:
        first = sorted(errors, key=lambda error: str(error.path))[0]
        raise ValueError(f"storyboard schema: {first.json_path}: {first.message}")
    if len(board["shots"]) != shot_count:
        raise ValueError(f"storyboard shot count: expected {shot_count}, got {len(board['shots'])}")
    if [entry["number"] for entry in board["shots"]] != list(range(1, shot_count + 1)):
        raise ValueError("storyboard shot numbers must be sequential")
    from storycg.tags import canonical_tag
    for prop in board.get('props', []):
        for value in prop.get('description_tags', []):
            if APPEARANCE_EN.search(value.replace('_', ' ')) or APPEARANCE_ZH.search(value):
                raise ValueError(f"appearance description is forbidden in props: {value}")
    first = board['shots'][0]['shot']
    if first['narrative_role'] != 'establishing' or first['framing'] not in ('wide', 'medium'):
        raise ValueError("first shot must establish the environment: establishing role "
                         "with wide or medium framing (closeup forbidden)")
    derive_outfit_changes(board)
    previous = None
    for entry in board["shots"]:
        shot = entry['shot']
        if any(tag not in entry['scene']['tags'] for tag in shot.get('prop_tags', [])):
            raise ValueError('prop tag must remain in scene.tags after tag validation')
        if previous and (shot['framing'], shot['camera_angle']) == (previous['framing'], previous['camera_angle']) and not shot['composition_reason'].strip():
            raise ValueError("adjacent shots need different framing/angle or a composition_reason")
        if board.get('scope_version') in (1, 2):
            if previous and (shot['expression'],shot['action'])==(previous['expression'],previous['action']) and not shot.get('unchanged_person_reason','').strip():
                raise ValueError('adjacent shots need expression/action change or unchanged_person_reason')
        previous = shot
        from storycg.identity_enrichment import category_for, load_rules
        rules=load_rules()
        clothing_for_check=copy.deepcopy(entry['outfit'])
        clothing_for_check['tags']=[tag for tag in entry['outfit']['tags'] if (category_for(canonical_tag(tag),rules) or {}).get('name') not in ('accessories','accessory_color')]
        action_fields = {'pose','action','description','spatial_relation','change_from_previous','tags'}
        values = [(v, False) for v in _strings({'scene':entry['scene'],'outfit':clothing_for_check})]
        values += [(v, key in action_fields) for key, field in entry['shot'].items() for v in _strings(field)]
        for value, body_action in values:
            if appearance_forbidden(value, body_action=body_action):
                raise ValueError(f"appearance description is forbidden in storyboard: {value}")
            if identity:
                forbidden = [identity.get("character_tag"), identity.get("work_tag"),
                             identity.get("input_name")]
                if any(term and re.search(rf"(?<!\w){re.escape(term.replace('_', ' '))}(?!\w)",
                                          value.replace("_", " "), re.I)
                       for term in forbidden):
                    raise ValueError(f"identity term is forbidden in storyboard: {value}")
    return board


def plan_story(job_dir: str | Path, story: str, identity: dict, shot_count: int,
               model_url: str, *, max_calls: int = 5, retries: int = 5,
               client: httpx.Client | None = None, config: dict | None = None,
               tag_registry=None, feedback: str | None = None) -> dict:
    job_dir = Path(job_dir)
    plan_path = job_dir / "storyboard.json"
    if plan_path.exists():
        board = json.loads(plan_path.read_text(encoding="utf-8"))
        saved_changes = [entry.get('outfit', {}).get('change_at_shot') for entry in board.get('shots', [])]
        validate_storyboard(board, shot_count, identity)
        if [entry['outfit']['change_at_shot'] for entry in board['shots']] != saved_changes:
            save_json_atomic(plan_path, board)  # persist the re-derived outfit changes
        from storycg.shot_vocab import validate_shot_vocab
        validate_shot_vocab(board, identity.get('identity_tier'), config)
        from storycg.story_rules import validate_story_rules
        return validate_story_rules(board, allow_obscured=(config or {}).get("allow_obscured_protagonist", False))
    if client is None:
        with httpx.Client(base_url=model_url.rstrip("/"), timeout=180,
                          trust_env=False) as owned:
            return plan_story(job_dir, story, identity, shot_count, model_url,
                              max_calls=max_calls, retries=retries, client=owned, config=config, tag_registry=tag_registry, feedback=feedback)
    model_response = client.get("/models")
    model_response.raise_for_status()
    models = model_response.json()["data"]
    available = [item for item in models if item.get("loaded", True) and
                 "chat" in item.get("capabilities", ["chat"])]
    if not available:
        raise ValueError("model service has no loaded chat model")
    model = available[0]["id"]
    log_path = job_dir / "plan_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"calls": []}
    instructions = (
        "Return only schema JSON. Plan exactly " + str(shot_count) + " visually causal shots. "
        "Descriptions/action/expression are concise English for display and audit ONLY. "
        "Only tags arrays enter the image prompt. Use REAL Danbooru tags, not invented phrases. "
        "No character names, franchises or physical appearance tags in any state. "
        "Body parts may occur only as direct targets of the current physical action in pose, "
        "spatial_relation and change_from_previous. Preserve the action's contact and direction. "
        "Keep each body-target field as one plain action clause; place no other clauses or "
        "appearance adjectives in that field. "
        "Never describe body-part appearance, color, shape, hair, skin, body shape or height. "
        "Eye states and actions on the eyes (closed eyes, half-closed eyes, one eye closed, "
        "wiping eyes, tears) are allowed in action or expression fields; eye color, shape and "
        "size remain forbidden. "
        "Use emotions such as surprised, confused, smile in shot.tags. "
        "scene.tags are concrete setting/time/weather/light tags; outfit.tags are clothing tags. Clothing-context hair ornaments/accessories belong in outfit.tags and are the sole exception to the hair-word ban; do not change anatomical hair tags. "
        "First shot has narrative_role establishing and owns the location. Use wide when this "
        "tier's vocabulary has a STABLE wide combo, or when every other shot is STABLE so the "
        "single SOMETIMES quota can be spent on the first shot; otherwise open with a STABLE "
        "medium combo and let scene tags carry the environment (never closeup first; medium "
        "object cap still applies). Vary framing and camera_angle "
        "between adjacent shots; identical pairs require a meaningful composition_reason. "
        "wide establishes place; medium focuses an interaction; closeup emphasizes reaction/detail. "
        "Props are background only. Put at most ONE ordinary object tag in shot.prop_tags, "
        "or use [] when absent. Include every depicted prop there; scene.tags describe the setting, "
        "time, weather and light, not extra objects. The script adds prop_tags to scene.tags. "
        "Scene object budgets by framing: closeup keeps only setting/place/time tags and NO objects; "
        "medium at most 2 object tags; full and wide at most 3. Never place action-invoking objects "
        "(computer, keyboard, monitor, laptop, smartphone, book, cup, mug, pen, musical instrument) "
        "in scene.tags unless that object is this shot's single declared prop. "
        "Actions that imply closed eyes (sleeping, sleeping on desk) must never pair with open eyes; "
        "the assembler adds the closed-eyes companion tag automatically. "
        "Do not register objects, bind holders, or track ownership, state or left/right hands. "
        "Keep setting and clothes continuous. Preserve exact temporal/weather facts and causal ending. "
        "scene.negative_tags MUST exclude incompatible weather/elements, including ongoing weather "
        "when the story states it has ended. No captions or dialogue. "
        "v2 has one main person; others only anonymous backs, cropped parts or offscreen. "
        "If the plot relies on mistaken possession, object ownership or mistaken identity, "
        "rewrite it at the storyboard stage so actions and expressions convey the key change. "
        "Record that adaptation in story_note as one concise Chinese sentence. If the inference "
        "cannot be expressed this way, continue planning and state in story_note that this part "
        "may not be readable. Otherwise set story_note to an empty string."
    )
    from storycg.story_rules import RULE_PROMPT
    instructions += RULE_PROMPT + CONTRACT_PROMPT + (' D25: scope_version=2. Key story changes must be expressed in expression/action. Each shot permits at most ONE prop tag. Adjacent shots must change expression or action; if both stay the same, provide a meaningful unchanged_person_reason, otherwise use an empty reason.')
    from storycg.shot_vocab import prompt_section
    instructions += prompt_section(identity.get('identity_tier'), config)
    from storycg.shot_contract import planner_schema, materialize
    schema=planner_schema(production_schema())
    outfit_tags = [item["tag"].replace("_", " ") for item in
                   identity.get("default_outfit_tags", [])]
    base_user_content = story
    if outfit_tags:
        base_user_content += ("\nSourced canonical outfit tags (clothing only): " +
                              ", ".join(outfit_tags) +
                              ". Use these in clothing_state unless the story changes clothes.")
    if feedback:
        base_user_content += "\nDiagnosis from actual generated images; rewrite to address it:\n" + feedback
    streak_rule = None
    streak = 0
    attempts = 0
    for _ in range(min(retries, max(0, max_calls-len(log['calls'])))):
        attempts += 1
        if len(log["calls"]) >= max_calls:
            raise ValueError("LLM call budget exhausted")
        gate=None
        if (config or {}).get("repair_cycle_enabled"):
            from storycg.budget import Budget
            from storycg.cycle import settings
            gate=Budget(job_dir,settings());gate.check(llm=1)
        call = {"model": model, "status": "started", "planning_contract": "single_moment_compiled_v2"}
        log["calls"].append(call)
        save_json_atomic(log_path, log)
        try:
            previous = log["calls"][-2] if len(log["calls"]) > 1 else None
            user_content = base_user_content
            if previous and previous.get("error"):
                user_content += ("\nThe previous storyboard was rejected: " + previous["error"] +
                                 f"\nRewrite all {shot_count} shots to satisfy the schema and remove every "
                                 "appearance attributes. Preserve physical actions and their body-part targets, "
                                 "contact and direction; never replace an action to avoid an anatomy word. "
                                 "Keep each body-target field as one plain action clause; place no other clauses "
                                 "or appearance adjectives in that field. "
                                 "Use emotion words only for expression. "
                                 "Do not copy an invalid field unchanged. Previous invalid JSON:\n" + previous.get("raw", ""))
            response = client.post("/chat/completions", timeout=min(180,gate.remaining_seconds()) if gate else 180, json={
                "model": model, "temperature": 0.2, "max_tokens": 6500,
                "messages": [{"role": "system", "content": instructions},
                             {"role": "user", "content": user_content}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "storyboard", "strict": True, "schema": schema}},
            })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            call["raw"] = content
            board = json.loads(content)
            schema_errors = list(Draft202012Validator(schema).iter_errors(board))
            if schema_errors:
                raise ValueError(f"storyboard schema: {schema_errors[0].json_path}: {schema_errors[0].message}")
            model_changes = [entry.get('outfit', {}).get('change_at_shot')
                             for entry in board.get('shots', [])]
            def record_outfit_changes():
                call['outfit_changes'] = [
                    {'shot': entry['number'],
                     'model_value': model_changes[index] if index < len(model_changes) else None,
                     'derived': entry['outfit'].get('change_at_shot')}
                    for index, entry in enumerate(board.get('shots', []))]
            board = materialize(board)
            # Later HTTP/schema checks may trigger a retry after the current
            # action has already been resolved safely. Keep that action and its
            # context, rather than let a new model response replace its meaning.
            anchors = {}
            for prior in log['calls'][:-1]:
                for record in prior.get('tag_validation', []):
                    if record.get('action') not in ('pose_omitted', 'pose_replaced'):
                        continue
                    match = re.fullmatch(r'\$\.shots\[(\d+)\]\.shot\.pose', record.get('pose_field', ''))
                    if match:
                        anchors.setdefault(int(match[1]), record)
            for index, anchor in anchors.items():
                shot = board['shots'][index]['shot']
                from storycg.tags import canonical_tag
                preserved = anchor.get('replacement') or canonical_tag(anchor['input'])
                call.setdefault('pose_anchors', []).append({'shot': index + 1,
                    'input_pose': shot['pose'], 'preserved_pose': preserved,
                    'reason': 'preserve previously resolved action across a planning retry'})
                shot.update(pose=preserved, action=preserved, description=preserved,
                            tags=list(dict.fromkeys([preserved, shot['expression']])))
                for key in ('spatial_relation','change_from_previous'):
                    shot[key] = anchor['semantic_context'][key]
            # Reject appearance/identity additions before discarding unknown tags.
            record_outfit_changes()
            validate_storyboard(board, shot_count, identity, config)
            from storycg.tags import TagRegistry, validate_tag_lists
            if tag_registry is None:
                with httpx.Client(base_url="https://danbooru.donmai.us", timeout=30, trust_env=False) as tag_client:
                    registry = TagRegistry(job_dir / "cache" / "tags", client=tag_client)
                    try:
                        validate_tag_lists(board, registry, config, drop_unknown=True)
                    finally:
                        call['tag_validation'] = registry.records
            else:
                before = len(tag_registry.records)
                try:
                    validate_tag_lists(board, tag_registry, config, drop_unknown=True)
                finally:
                    call['tag_validation'] = tag_registry.records[before:]
            from storycg.shot_vocab import validate_shot_vocab
            record_outfit_changes()
            board = validate_storyboard(board, shot_count, identity, config)
            call['shot_vocab'] = validate_shot_vocab(board, identity.get('identity_tier'), config)
            from storycg.story_rules import validate_story_rules
            validate_story_rules(board, allow_obscured=(config or {}).get('allow_obscured_protagonist', False))
        except (ValueError, KeyError, IndexError, httpx.HTTPError) as exc:
            call["status"] = "invalid"
            call["error"] = f"{type(exc).__name__}: {exc}"
            from storycg.story_rules import StoryRuleViolation
            rule_id = exc.rule_id if isinstance(exc, StoryRuleViolation) else None
            call['rule_id'] = rule_id
            if rule_id:
                streak = streak + 1 if rule_id == streak_rule else 1
            else:
                streak = 0
            streak_rule = rule_id
            call['consecutive_rule_rejections'] = streak
            save_json_atomic(log_path, log)
            if streak >= 3:
                raise ValueError(f"分镜连续 3 次违反同一规则 {rule_id}，已停止；{exc}；日志：{log_path}") from exc
            continue
        call["status"] = "valid"
        save_json_atomic(log_path, log)
        save_json_atomic(plan_path, board)
        return board
    raise ValueError(f"storyboard failed validation after {attempts} calls (limit {max_calls}); last error: {log['calls'][-1].get('error', 'no call available') if log['calls'] else 'no call available'}; see {log_path}")
