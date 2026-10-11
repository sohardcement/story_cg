"""Schema-bound runtime storyboarding with appearance exclusion."""

import json
import copy
import re
from pathlib import Path

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


# V2-33: which rule a body-target value broke, for the error text only. appearance_forbidden still decides.
BODY_TARGET_NOUNS = frozenset({'face', 'eyes', 'eye', 'mouth', 'ears', 'ear', 'lips', 'lip', 'nose',
                               'hands', 'hand', 'fingers', 'finger', 'palms', 'palm'})
SHAPE_SIZE_WORDS = frozenset({'big', 'large', 'small', 'tiny', 'round', 'narrow', 'wide', 'slanted', 'tareme',
                              'tsurime', 'droopy', 'sharp', 'almond', 'long', 'short'})


def body_target_violation(value):
    """Kind of the body-target rule breach in `value`, or None when appearance_forbidden passes it.

    'appearance': the value names appearance (a Chinese appearance word, a hair/build word, a colour or a
    shape/size word, or an appearance noun other than a body target).
    'clause': no appearance word, but the value is not a single plain action clause (extra clauses, reasons,
    a second action). The field then accepts one action clause only.
    """
    if not appearance_forbidden(value, body_action=True):
        return None
    if APPEARANCE_ZH.search(value):
        return 'appearance'
    from storycg.tags import colour_words
    normalized = re.sub(r'[_-]', ' ', re.sub(r'\beye[ -]level\b', '', value, flags=re.I))
    words = {word.lower() for word in re.findall(r'[A-Za-z]+', normalized)}
    if words & (set(colour_words()) | SHAPE_SIZE_WORDS):
        return 'appearance'
    hits = {match.group(0).lower() for match in APPEARANCE_EN.finditer(normalized)}
    return 'appearance' if hits - BODY_TARGET_NOUNS else 'clause'


# V2-40: a shot is one still instant, so spatial_relation may not chain a second action with an order word.
# "after" and "before" stay allowed (they give the state's cause, "relieved after the scare"); "next to" is a place.
SEQUENCE_WORDS = re.compile(r"\b(?:then|afterwards?|subsequently|after\s+that|followed\s+by|finally|next(?!\s+to\b))\b",
                            re.I)


def sequence_violation(value):
    """True when a spatial_relation value orders two actions (V2-40): one shot shows one instant."""
    return bool(SEQUENCE_WORDS.search(value or ''))


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
        if sequence_violation(shot.get('spatial_relation', '')):
            raise ValueError(f"single instant: shot {entry.get('number')} spatial_relation orders two actions: {shot['spatial_relation']}")
    from storycg.prompt_rules import companion_conflicts
    conflicts = companion_conflicts(board, config)
    if conflicts:
        detail = '; '.join(f"shot {c['shot']} tag {c['trigger']} requires {', '.join(c['require'])} but has {', '.join(c['conflicts'])}"
                           for c in conflicts)
        raise ValueError('action companion tag conflict: ' + detail)
    validate_contract(board)
    errors = list(Draft202012Validator(production_schema() if board.get("schema_version")==2 else STORYBOARD_SCHEMA).iter_errors(board))
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
    if first['framing'] != 'wide' or first['narrative_role'] != 'establishing':
        raise ValueError("first shot must establish the environment: wide framing and establishing role")
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


FIRST_SHOT_RULE = ("First shot MUST be wide with narrative_role establishing. ")


def writing_rules(shot_count: int) -> str:
    """Storyboard writing rules shared by plan_story and `storycg beats-guide` (V2-10).

    Excludes the tier-dependent vocabulary section (prompt_section), which only plan_story adds.
    """
    instructions = (
        "Return only schema JSON. Plan exactly " + str(shot_count) + " visually causal shots. "
        "Prose fields (narrative_role, composition_reason, spatial_relation, change_from_previous, story_note) are concise English for display and audit ONLY. "
        "Only tags arrays enter the image prompt. Use REAL Danbooru tags, not invented phrases. "
        "No character names, franchises or physical appearance tags in any state. "
        "Body parts may occur only as direct targets of the current physical action in pose, "
        "spatial_relation and change_from_previous. Preserve the action's contact and direction. "
        "spatial_relation names the one instant this shot depicts; no then or other order words there, "
        "and two actions in order are two shots. "
        "Keep each body-target field as one plain action clause; place no other clauses or "
        "appearance adjectives in that field. "
        "Never describe body-part appearance, color, shape, hair, skin, body shape or height. "
        "Eye states (closed eyes, half-closed eyes, one eye closed, tears) go in expression; an eye action "
        "listed in the vocabulary (wiping_tears) may be the pose; eye color, shape and "
        "size remain forbidden. "
        "Put the emotion in shot.expression as one tag from the expression list (surprised, confused, smile, ...). "
        "scene.tags are concrete setting/time/weather/light tags; outfit.tags are clothing tags. Clothing-context hair ornaments/accessories belong in outfit.tags and are the sole exception to the hair-word ban; do not change anatomical hair tags. "
        + FIRST_SHOT_RULE + "Vary framing and camera_angle "
        "between adjacent shots; identical pairs require a meaningful composition_reason. "
        "wide establishes place; medium focuses an interaction; closeup emphasizes reaction/detail. "
        "Props are plain tags: not registered, not bound to a holder. An object or animal the story needs drawn in this shot "
        "(held, found or looked at) must be written in this shot's prop_tags; words in spatial_relation or description alone "
        "are not drawn. Put at most ONE ordinary object tag in shot.prop_tags, "
        "or use [] when absent. Include every depicted prop there; scene.tags describe the setting, "
        "time, weather and light, not extra objects. The script adds prop_tags to scene.tags. "
        "Scene object budgets by framing: closeup keeps only setting/place/time/weather/light tags plus the one "
        "shot.prop_tags entry; every other object is dropped; "
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
    return instructions


def saved_storyboard(job_dir: str | Path, shot_count: int, identity: dict, config: dict | None = None) -> dict:
    """V2-29: the job's storyboard is the one `beats compile` wrote (storyboard.json). No model is called.

    The saved board is re-validated on every use; outfit changes are re-derived and persisted if they moved.
    A job with no compiled storyboard is refused with the next step to take.
    """
    plan_path = Path(job_dir) / "storyboard.json"
    if not plan_path.exists():
        raise ValueError("no compiled storyboard for this job: write the beats file with beats-guide, "
                         "check it with compile, then run make --beats")
    board = json.loads(plan_path.read_text(encoding="utf-8"))
    saved_changes = [entry.get('outfit', {}).get('change_at_shot') for entry in board.get('shots', [])]
    validate_storyboard(board, shot_count, identity)
    if [entry['outfit']['change_at_shot'] for entry in board['shots']] != saved_changes:
        save_json_atomic(plan_path, board)  # persist the re-derived outfit changes
    from storycg.shot_vocab import validate_shot_vocab
    validate_shot_vocab(board, config)
    from storycg.story_rules import validate_story_rules
    return validate_story_rules(board, allow_obscured=(config or {}).get("allow_obscured_protagonist", False))
