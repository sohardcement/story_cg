"""Schema-bound runtime storyboarding with appearance exclusion."""

import json
import re
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator

from storycg.state import save_json_atomic


TEXT = {"type": "string", "minLength": 1}
SCENE = {"type": "object", "additionalProperties": False,
         "required": ["location", "time", "weather", "lighting", "environment_change"],
         "properties": {key: TEXT for key in
                        ["location", "time", "weather", "lighting", "environment_change"]}}
OUTFIT = {"type": "object", "additionalProperties": False,
          "required": ["clothing_state", "change_at_shot"],
          "properties": {"clothing_state": TEXT,
                         "change_at_shot": {"type": ["integer", "null"], "minimum": 1}}}
PROP = {"type": "object", "additionalProperties": False,
        "required": ["name", "held_by", "position"],
        "properties": {key: TEXT for key in ["name", "held_by", "position"]}}
SHOT = {"type": "object", "additionalProperties": False,
        "required": ["narrative_role", "action", "expression", "key_props",
                     "spatial_relation", "framing", "camera_angle", "change_from_previous"],
        "properties": {**{key: TEXT for key in ["narrative_role", "action", "expression",
                                                "spatial_relation", "camera_angle",
                                                "change_from_previous"]},
                       "key_props": {"type": "array", "items": PROP},
                       "framing": {"enum": ["wide", "full", "medium", "closeup"]}}}
STORYBOARD_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                     "type": "object", "additionalProperties": False,
                     "required": ["shots"], "properties": {"shots": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False,
                         "required": ["number", "scene", "outfit", "shot"],
                         "properties": {"number": {"type": "integer", "minimum": 1},
                                        "scene": SCENE, "outfit": OUTFIT, "shot": SHOT}}}}}

APPEARANCE_EN = re.compile(
    r"\b(?:hair|haired|hairstyle|bangs|twintails?|ponytails?|braids?|eyes?|eyed|iris|pupils?|"
    r"eyelashes|eyebrows?|ears?|skin|complexion|face|facial features|body type|"
    r"height|stature|build|figure|petite|elf|horns?|tails?|fangs?|freckles|moles?|"
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
    for entry in board["shots"]:
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
               model_url: str, *, max_calls: int = 20, retries: int = 3,
               client: httpx.Client | None = None) -> dict:
    job_dir = Path(job_dir)
    plan_path = job_dir / "storyboard.json"
    if plan_path.exists():
        board = json.loads(plan_path.read_text(encoding="utf-8"))
        return validate_storyboard(board, shot_count, identity)
    if client is None:
        with httpx.Client(base_url=model_url.rstrip("/"), timeout=180,
                          trust_env=False) as owned:
            return plan_story(job_dir, story, identity, shot_count, model_url,
                              max_calls=max_calls, retries=retries, client=owned)
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
        "Return only JSON matching the schema. Plan a sequence of exactly " + str(shot_count) +
        " distinct visual shots for one named protagonist. Write all state strings in concise English "
        "suitable for image generation. Refer to the main character only as 'protagonist'. "
        "Do not include any character name, franchise, identity tag, physical appearance, "
        "hair, eyes, ears, skin, height, face, or body descriptors. Other people, if needed, "
        "must be anonymous backs, passersby, cropped body parts, or offscreen. Preserve the "
        "story's causal event and ending. Specify what each important prop is, who holds it, "
        "and where it is. Keep outfit and setting continuous; use change_at_shot only when "
        "clothes actually change. Make the event legible without captions or dialogue. "
        "For expression use only emotion words such as surprised, puzzled, or relieved; "
        "never describe eyes, eyebrows, brow, face, skin, hair, ears, or body. "
        "Preserve the user's exact temporal and weather facts, place, prop ownership, "
        "and causal outcome. Do not introduce unrelated props or change the weather."
    )
    outfit_tags = [item["tag"].replace("_", " ") for item in
                   identity.get("default_outfit_tags", [])]
    base_user_content = story
    if outfit_tags:
        base_user_content += ("\nSourced canonical outfit tags (clothing only): " +
                              ", ".join(outfit_tags) +
                              ". Use these in clothing_state unless the story changes clothes.")
    for _ in range(retries):
        if len(log["calls"]) >= max_calls:
            raise ValueError("LLM call budget exhausted")
        call = {"model": model, "status": "started"}
        log["calls"].append(call)
        save_json_atomic(log_path, log)
        try:
            previous = log["calls"][-2] if len(log["calls"]) > 1 else None
            user_content = base_user_content
            if previous and previous.get("error"):
                user_content += ("\nThe previous storyboard was rejected: " + previous["error"] +
                                 f"\nRewrite all {shot_count} shots to satisfy the schema and remove every "
                                 "appearance/anatomy description. Use emotion words only for expression.")
            response = client.post("/chat/completions", json={
                "model": model, "temperature": 0.2, "max_tokens": 4000,
                "messages": [{"role": "system", "content": instructions},
                             {"role": "user", "content": user_content}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "storyboard", "strict": True, "schema": STORYBOARD_SCHEMA}},
            })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            call["raw"] = content
            board = validate_storyboard(json.loads(content), shot_count, identity)
        except (ValueError, KeyError, IndexError, httpx.HTTPError) as exc:
            call["status"] = "invalid"
            call["error"] = f"{type(exc).__name__}: {exc}"
            save_json_atomic(log_path, log)
            continue
        call["status"] = "valid"
        save_json_atomic(log_path, log)
        save_json_atomic(plan_path, board)
        return board
    raise ValueError(f"storyboard failed validation after {retries} calls; see {log_path}")
