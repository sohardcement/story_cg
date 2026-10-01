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
PROP_STATE = {"type": "object", "additionalProperties": False,
              "required": ["id", "kind", "description_tags"],
              "properties": {"id": TEXT, "kind": TEXT,
                             "role": {"enum": ["key", "group"], "description": "key is an individually tracked story object. group is homogeneous background/display items with no individual holder, owner or state distinction; otherwise register separate key objects."},
                             "description_tags": {**TAGS, "minItems": 1}}}
PROP = {"type": "object", "additionalProperties": False,
        "required": ["prop_id", "held_by", "position", "relation_tags", "location_tags"],
        "properties": {"prop_id": TEXT, "held_by": {"enum": ["subject", "other", "none", "offscreen"]},
                       "position": TEXT, "owner": TEXT, "state": TEXT,
                       "relation_tags": TAGS, "location_tags": TAGS}}
SHOT = {"type": "object", "additionalProperties": False,
        "required": ["narrative_role", "description", "action", "expression", "key_props", "tags",
                     "spatial_relation", "framing", "camera_angle", "change_from_previous", "composition_reason"],
        "properties": {**{key: TEXT for key in ["narrative_role", "description", "action", "expression",
                                                "spatial_relation", "change_from_previous"]},
                       "composition_reason": {"type": "string"},
                       "tags": {**TAGS, "minItems": 1},
                       "key_props": {"type": "array", "items": PROP},
                       "camera_angle": {"enum": ["front", "above", "below", "side", "behind", "dutch"]},
                       "framing": {"enum": ["wide", "full", "medium", "closeup"]}}}
STORYBOARD_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                     "type": "object", "additionalProperties": False,
                     "required": ["props", "shots"], "properties": {
                         "props": {"type": "array", "items": PROP_STATE},
                         "shots": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False,
                         "required": ["number", "scene", "outfit", "shot"],
                         "properties": {"number": {"type": "integer", "minimum": 1},
                                        "scene": SCENE, "outfit": OUTFIT, "shot": SHOT}}}}}

# Optional on legacy boards; production planning requires explicit visibility/ownership.
SHOT['properties'].update({
    'subject_visibility':{'enum':['clear','obscured']},
    'other_people':{'type':'array','items':{'enum':['passerby','back_view','offscreen','partial']}},
    'hand_owners':{'type':'array','items':{'enum':['subject','anonymous_other']}}})
PROP['properties']['detached']={'type':'boolean', 'description': 'True only at the instant an object is airborne, flying away, falling or departing from the subject. False for placed/set-down props resting on a surface or at a location, even when separated from the subject.'}


def production_schema():
    schema=copy.deepcopy(STORYBOARD_SCHEMA)
    shot=schema['properties']['shots']['items']['properties']['shot']
    shot['required'] += ['subject_visibility','other_people','hand_owners']
    shot['properties']['key_props']['items']['required'].append('detached')
    shot['properties']['key_props']['items']['required'] += ['owner', 'state']
    schema['properties']['props']['items']['required'].append('role')
    return schema


APPEARANCE_EN = re.compile(
    r"\b(?:hair|haired|hairstyle|bangs|twintails?|ponytails?|braids?|eyes?|eyed|iris|pupils?|"
    r"eyelashes|eyebrows?|ears?|skin|complexion|face|facial features|body type|"
    r"breasts?|bust|slender|muscular|plump|chubby|thin|fat|ahoge|height|stature|build|figure|petite|elf|horns?|tails?|fangs?|freckles|moles?|"
    r"lips?|nose|mouth)\b", re.I)
APPEARANCE_ZH = re.compile(
    r"头发|发色|发型|发丝|长发|短发|卷发|直发|银发|金发|黑发|白发|红发|蓝发|绿发|紫发|粉发|"
    r"刘海|马尾|辫子|瞳色|瞳孔|眼睛|蓝眸|绿眸|红眸|眉毛|耳朵|肤色|皮肤|脸型|面容|体型|身高|精灵耳|尾巴|角"
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def validate_storyboard(board: dict, shot_count: int, identity: dict | None = None) -> dict:
    errors = list(Draft202012Validator(STORYBOARD_SCHEMA).iter_errors(board))
    if errors:
        first = sorted(errors, key=lambda error: str(error.path))[0]
        raise ValueError(f"storyboard schema: {first.json_path}: {first.message}")
    if len(board["shots"]) != shot_count:
        raise ValueError(f"storyboard shot count: expected {shot_count}, got {len(board['shots'])}")
    if [entry["number"] for entry in board["shots"]] != list(range(1, shot_count + 1)):
        raise ValueError("storyboard shot numbers must be sequential")
    from storycg.tags import canonical_tag
    props = {p['id']: p for p in board['props']}
    if len(props) != len(board['props']):
        raise ValueError("prop ids must be unique")
    visual_attributes = re.compile(r"(?:^|_)(red|blue|green|yellow|orange|purple|pink|white|black|grey|gray|brown|silver|gold|multicolored|rainbow|striped|plaid|checkered|polka_dot|floral|argyle|camouflage|patterned)(?:_|$)")
    for prop in board['props']:
        for value in prop['description_tags']:
            if APPEARANCE_EN.search(value.replace('_', ' ')) or APPEARANCE_ZH.search(value):
                raise ValueError(f"appearance description is forbidden in props: {value}")
    first = board['shots'][0]['shot']
    if first['framing'] != 'wide' or first['narrative_role'] != 'establishing':
        raise ValueError("first shot must establish the environment: wide framing and establishing role")
    previous = None
    for entry in board["shots"]:
        shot = entry['shot']
        if previous and (shot['framing'], shot['camera_angle']) == (previous['framing'], previous['camera_angle']) and not shot['composition_reason'].strip():
            raise ValueError("adjacent shots need different framing/angle or a composition_reason")
        previous = shot
        for ref in shot['key_props']:
            if ref['prop_id'] not in props:
                raise ValueError(f"unknown prop reference: {ref['prop_id']}")
            if ref['held_by'] in ('subject', 'other') and not ref['relation_tags']:
                raise ValueError("held props need hand/object relation tags")
            if props[ref['prop_id']].get('role') == 'group' and (
                    ref['held_by'] not in ('none', 'offscreen') or ref.get('detached')
                    or ref.get('owner', 'none') != 'none'):
                raise ValueError("display group cannot have an individual holder/owner or detached state; register key props")
        if len({ref['prop_id'] for ref in shot['key_props']}) != len(shot['key_props']):
            raise ValueError("each prop may be referenced only once per shot")
        visible = [ref for ref in shot['key_props'] if ref['held_by'] != 'offscreen'
                   and props[ref['prop_id']].get('role', 'key') == 'key']
        for index, ref in enumerate(visible):
            for other in visible[index + 1:]:
                left, right = props[ref['prop_id']], props[other['prop_id']]
                if canonical_tag(left['kind']) != canonical_tag(right['kind']):
                    continue
                # Legacy boards use placement/contact as the state fallback.
                def relation_state(item):
                    return (item['held_by'], item.get('owner', 'none'),
                            item.get('state', item['position']), item.get('detached', False))
                if relation_state(ref) == relation_state(other):
                    continue
                def attributes(prop):
                    return frozenset(match.group(1) for tag in prop['description_tags']
                                     for match in visual_attributes.finditer(canonical_tag(tag)))
                a, b = attributes(left), attributes(right)
                if not a or not b or a == b:
                    raise ValueError("same-kind props in one shot with different holder/owner/state require distinct color or pattern tags")
        changed = entry["outfit"]["change_at_shot"]
        if changed is not None and changed != entry["number"]:
            raise ValueError("outfit change_at_shot must name the current shot")
        for value in _strings({key: entry[key] for key in ("scene", "outfit", "shot")}):
            normalized = re.sub(r"[_-]", " ", value)
            normalized = re.sub(r"\beye[ -]level\b", "", normalized, flags=re.I)
            if APPEARANCE_EN.search(normalized) or APPEARANCE_ZH.search(value):
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
        validate_storyboard(board,shot_count,identity)
        from storycg.story_rules import validate_story_rules
        return validate_story_rules(board,allow_obscured=(config or {}).get("allow_obscured_protagonist",False))
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
        "Descriptions/action/expression/position are concise English for display and audit ONLY. "
        "Only tags arrays enter the image prompt. Use REAL Danbooru tags, not invented phrases. "
        "No character names, franchises or physical appearance tags in any state. "
        "Never use the words face, eyes, mouth, hair, ears anywhere, even in description, "
        "composition_reason or spatial_relation. Say reaction closeup or surprised instead. "
        "Do not describe hair, eyes, ears, skin, body shape, height or facial anatomy. "
        "Use emotions such as surprised, confused, smile in shot.tags. "
        "scene.tags are concrete setting/time/weather/light tags; outfit.tags are clothing tags. "
        "First shot MUST be wide with narrative_role establishing. Vary framing and camera_angle "
        "between adjacent shots; identical pairs require a meaningful composition_reason. "
        "wide establishes place; medium focuses an interaction; closeup emphasizes reaction/detail. "
        "Register every key prop at top-level props with unique id, kind (a real object tag), "
        "role key for individually tracked story objects; use role group for homogeneous scene/display "
        "collections without individual ownership/state, or leave decoration in scene.tags. Groups "
        "must have held_by none, owner none and detached false. description_tags contain the object "
        "tag and concrete color/pattern tags. If a color-object compound tag does not exist, use "
        "separate REAL color and object tags; both must exist. Same-kind key props visible in the "
        "SAME shot with different holder, owner or state MUST have conspicuously different colors "
        "or patterns. Other same-kind objects do not require distinction. "
        "Each shot references prop_id with held_by and position, AND real relation_tags/location_tags "
        "that visually express contact and placement. Include owner (subject/other/none or stable "
        "anonymous owner id) and state (stable concise object state, identical for equivalent states). "
        "held_by subject means the main person. "
        "Use tags for holding/contact, pose, gesture and expression; omit irrelevant props per shot. "
        "Keep setting and clothes continuous. Preserve exact temporal/weather facts and causal ending. "
        "scene.negative_tags MUST exclude incompatible weather/elements, including ongoing weather "
        "when the story states it has ended. No captions or dialogue: visually reveal recognition "
        "of the error, its contrasting evidence and the corrective action. "
        "v1 has one main person; others only anonymous backs, cropped parts or offscreen."
    )
    from storycg.story_rules import RULE_PROMPT
    instructions += RULE_PROMPT
    schema=production_schema() if (config or {}).get("repair_cycle_enabled") else STORYBOARD_SCHEMA
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
        call = {"model": model, "status": "started"}
        log["calls"].append(call)
        save_json_atomic(log_path, log)
        try:
            previous = log["calls"][-2] if len(log["calls"]) > 1 else None
            user_content = base_user_content
            if previous and previous.get("error"):
                user_content += ("\nThe previous storyboard was rejected: " + previous["error"] +
                                 f"\nRewrite all {shot_count} shots to satisfy the schema and remove every "
                                 "appearance/anatomy description. Never use the words face, eyes, mouth, hair, ears "
                                 "anywhere in descriptions/action/spatial_relation/composition_reason. "
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
            schema_errors = list(Draft202012Validator(STORYBOARD_SCHEMA).iter_errors(board))
            if schema_errors:
                raise ValueError(f"storyboard schema: {schema_errors[0].message}")
            # Reject appearance/identity additions before discarding unknown tags.
            validate_storyboard(board, shot_count, identity)
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
            board = validate_storyboard(board, shot_count, identity)
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
        save_json_atomic(job_dir / "props.json", board["props"])
        return board
    raise ValueError(f"storyboard failed validation after {attempts} calls (limit {max_calls}); last error: {log['calls'][-1].get('error', 'no call available') if log['calls'] else 'no call available'}; see {log_path}")
