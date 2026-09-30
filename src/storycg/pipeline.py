"""Restartable four-shot story pipeline built on the P1-01 runner."""

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from storycg.assembly import assemble_prompt
from storycg.config import load_config
from storycg.identity import resolve_identity
from storycg.planning import plan_story
from storycg.runner import run_gen_one
from storycg.state import load_state, save_json_atomic


def new_job(job_dir: str | Path, character: str, story: str, shots: int = 4, *, generation_options: dict | None = None, generation_config: dict | None = None) -> dict:
    job_dir = Path(job_dir)
    if not 4 <= shots <= 6:
        raise ValueError("story jobs require 4 to 6 shots")
    expected = {"character": character.strip(), "story": story.strip(), "shot_count": shots}
    if not expected["character"] or not expected["story"]:
        raise ValueError("character and story are required")
    if generation_options:
        expected["generation_options"] = generation_options
    if generation_config is not None:
        expected["generation_config"] = generation_config
    path = job_dir / "job.json"
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if any(current.get(key) != value for key, value in expected.items()):
            raise ValueError("job already exists with different inputs")
        return current
    if job_dir.exists() and any(job_dir.iterdir()):
        raise ValueError("new job directory must be empty")
    record = {**expected, "job_id": job_dir.name,
              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_json_atomic(path, record)
    return record


def resolve_job(job_dir: str | Path) -> dict:
    job_dir = Path(job_dir)
    path = job_dir / "identity.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    identity = resolve_identity(job["character"], job_dir / "cache" / "danbooru")
    save_json_atomic(path, identity)
    return identity


def plan_job(job_dir: str | Path, config: dict) -> dict:
    job_dir = Path(job_dir)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    identity = resolve_job(job_dir)
    if identity["status"] != "locked":
        raise ValueError("identity requires clarification before planning")
    return plan_story(job_dir, job["story"], identity, job["shot_count"],
                      config["model_service_url"], max_calls=config.get("planning", {}).get("max_calls", 25),
                      retries=config.get("planning", {}).get("retries", 10), config=config)


def _model_manifest(config: dict, client) -> dict:
    def file_info(folder: str, name: str) -> dict:
        try:
            match = next((item for item in client.list_model_files(folder)
                          if item.get("name") == name), None)
            if match:
                return {"size": match.get("size"), "modified": match.get("modified")}
            return {"size": "unavailable", "modified": "unavailable"}
        except (AttributeError, httpx.HTTPError, ValueError):
            return {"size": "unavailable", "modified": "unavailable"}

    checkpoint = {"name": config["checkpoint"], "sha256": "unavailable",
                  "hash_reason": "ComfyUI checkpoint hash endpoint unavailable",
                  **file_info("checkpoints", config["checkpoint"])}
    loras = []
    for item in config["style_loras"]:
        name = item["name"]
        try:
            metadata = client.get_model_metadata("loras", name)
        except (AttributeError, httpx.HTTPError, ValueError):
            metadata = {}
        loras.append({**item, "sshs_model_hash": metadata.get("sshs_model_hash", "unavailable"),
                      "sshs_legacy_hash": metadata.get("sshs_legacy_hash", "unavailable"),
                      **file_info("loras", name)})
    return {"checkpoint": checkpoint, "loras": loras}


def write_story_manifest(job_dir: str | Path, config: dict, client) -> dict:
    job_dir = Path(job_dir)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    identity = json.loads((job_dir / "identity.json").read_text(encoding="utf-8"))
    board = json.loads((job_dir / "storyboard.json").read_text(encoding="utf-8"))
    state = load_state(job_dir)
    log_path = job_dir / "plan_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"calls": []}
    images = []
    layouts = [{"shot_id": a["shot_id"], "prompt_id": a["prompt_id"], "seed": a["seed"],
                "reference": a.get("reference"), "workflow": a["workflow"], **a["image"]}
               for a in state["attempts"] if a["shot_id"].startswith("layout") and a["status"] == "completed"]
    for entry in board["shots"]:
        shot_id = f'shot{entry["number"]:02d}'
        matching = [attempt for attempt in state["attempts"]
                    if attempt["shot_id"] == shot_id and attempt["status"] == "completed"]
        if not matching:
            continue
        attempt = matching[-1]
        parts = assemble_prompt(identity, entry, props=board["props"], config=config)
        workflow = attempt["workflow"]
        images.append({"shot_id": shot_id, "attempt_number": attempt["attempt_number"],
                       "prompt_id": attempt["prompt_id"], "filename_prefix": attempt["filename_prefix"],
                       "identity_block": parts["identity_block"], "prompt_parts": parts,
                       "positive_prompt": workflow["4"]["inputs"]["text"],
                       "negative_prompt": workflow["5"]["inputs"]["text"],
                       "seed": attempt["seed"], "sampler": workflow["7"]["inputs"],
                       "resolution": workflow["6"]["inputs"], "reference": attempt.get("reference"),
                       "created_at": attempt["created_at"], "completed_at": attempt["completed_at"], **attempt["image"]})
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"])).total_seconds()
    prior_path = job_dir / "manifest.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    manifest = {"job_id": job["job_id"], "character_input": job["character"],
                "story": job["story"], "identity_file": "identity.json",
                "storyboard_file": "storyboard.json", "models": _model_manifest(config, client),
                "style": config.get("style_snapshot"),
                "identity_control": config.get("identity_control"), "layout_mode": config.get("layout_mode", "none"),
                "layout_images": layouts, "reference_models": config.get("reference_models"),
                "reference_relationship": {"portrait": json.loads((job_dir / "portrait.json").read_text()) if (job_dir / "portrait.json").exists() else None},
                "review_records": prior.get("review_records", []),
                "images": images, "usage": {"generated_images": state.get("stats", {}).get("generated_images", 0),
                                             "submission_attempts": state.get("stats", {}).get("submission_attempts", 0),
                                             "llm_calls": len(log["calls"]),
                                             "review_calls": prior.get("usage", {}).get("review_calls", 0),
                                             "elapsed_seconds": round(elapsed, 1)}}
    for key in ("review_call_seconds", "review_elapsed_seconds"):
        if key in prior.get("usage", {}):
            manifest["usage"][key] = prior["usage"][key]
    save_json_atomic(job_dir / "manifest.json", manifest)
    return manifest


def make_contact_sheet(job_dir: str | Path, review_dir: str | Path) -> Path:
    job_dir = Path(job_dir)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    board = json.loads((job_dir / "storyboard.json").read_text(encoding="utf-8"))
    state = load_state(job_dir)
    cell_w, cell_h, margin = 600, 500, 18
    columns = 2
    rows = (len(board["shots"]) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_w + (columns + 1) * margin,
                              rows * cell_h + (rows + 1) * margin), "#e8e8e8")
    font = None
    for name in ("LiberationSans-Bold.ttf", "NotoSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, 80)
            break
        except OSError:
            continue
    if font is None:
        raise OSError("no scalable bold font available for contact-sheet numbers")
    for index, entry in enumerate(board["shots"]):
        shot_id = f'shot{entry["number"]:02d}'
        attempts = [a for a in state["attempts"] if a["shot_id"] == shot_id and
                    a["status"] == "completed"]
        if not attempts:
            raise ValueError(f"missing completed image for {shot_id}")
        path = job_dir / attempts[-1]["image"]["path"]
        with Image.open(path) as source:
            thumb = ImageOps.contain(source.convert("RGB"), (cell_w, cell_h))
        x = margin + (index % columns) * (cell_w + margin)
        y = margin + (index // columns) * (cell_h + margin)
        sheet.paste(thumb, (x + (cell_w - thumb.width) // 2,
                            y + (cell_h - thumb.height) // 2))
        draw = ImageDraw.Draw(sheet)
        draw.rounded_rectangle((x + 12, y + 12, x + 100, y + 105), radius=12, fill="#101010")
        bounds = draw.textbbox((0, 0), str(entry["number"]), font=font)
        left = x + 12 + (88 - (bounds[2] - bounds[0])) // 2 - bounds[0]
        top = y + 12 + (93 - (bounds[3] - bounds[1])) // 2 - bounds[1]
        draw.text((left, top), str(entry["number"]), font=font, fill="white")
    path = job_dir / "contact_sheet.jpg"
    sheet.save(path, "JPEG", quality=92, subsampling=0)
    review_dir = Path(review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)
    short = re.sub(r"[^\w\u3400-\u9fff]+", "", job["story"])[:30]
    review_path = review_dir / f'{job["character"]}-{short}.jpg'
    if review_path.exists() and review_path.read_bytes() != path.read_bytes():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        review_path = review_dir / f'{job["character"]}-{short}-{job_dir.name}-{digest}.jpg'
    if not review_path.exists():
        review_path.write_bytes(path.read_bytes())
    return review_path


def run_story_job(job_dir: str | Path, config: dict, client,
                  *, review_dir: str | Path = "review", repair_diagnoses: dict | None = None) -> dict:
    job_dir = Path(job_dir)
    identity = resolve_job(job_dir)
    if identity["status"] != "locked":
        raise ValueError("identity requires clarification before generation")
    from storycg.reference import ensure_portrait
    portrait = ensure_portrait(job_dir, identity, config, client)
    board = plan_job(job_dir, config)
    if (job_dir / "state.json").exists():
        config = load_state(job_dir)["config"]
    else:
        config = copy.deepcopy(config)
        config["budget"]["max_images_per_job"] = min(max(8, 2 * len(board["shots"])), config["budget"]["max_images_per_job"])
        config["budget"]["max_job_minutes"] = min(60, config["budget"]["max_job_minutes"])
    from storycg.controls import apply_options, portrait_reference, upload_reference
    from storycg.reference import edit_instruction
    job = json.loads((job_dir / 'job.json').read_text())
    config = apply_options(config, job.get('generation_options'))
    config.pop('generation_reference', None)
    config['auto_retry_failed'] = False
    control = config['identity_control']
    face_ref = (portrait_reference(job_dir, portrait, 'face', client, 'ipadapter', control['ipadapter_strength'])
                if control['mechanism'] == 'ipadapter' else None)
    full_ref = (portrait_reference(job_dir, portrait, 'image', client, 'qwen_edit', 1.0)
                if config['layout_mode'] == 'qwen' else None)
    state = None
    for entry in board["shots"]:
        parts = assemble_prompt(identity, entry, props=board["props"], config=config)
        shot_config = copy.deepcopy(config)
        shot_config["negative_prompt"] = ", ".join(filter(None, [config["negative_prompt"], parts["negative_tags"]]))
        shot_id = f'shot{entry["number"]:02d}'
        relation = face_ref
        if full_ref:
            layout_id = f'layout{entry["number"]:02d}'
            state = run_gen_one(job_dir, layout_id, edit_instruction(entry, board['props']),
                                parts['framing'], config, client, generation_reference=full_ref)
            layout = next(a for a in reversed(state['attempts']) if a['shot_id'] == layout_id)
            if layout['status'] != 'completed':
                break
            relation = {'mechanism': 'img2img', 'strength': config.get('layout_denoise', 0.55),
                        **layout['image'], 'prompt_id': layout['prompt_id'], 'source_task': str(job_dir.resolve()),
                        'uploaded_image': upload_reference(job_dir, job_dir/layout['image']['path'], client, expected_sha256=layout['image']['sha256']),
                        'upstream_reference': full_ref, 'identity_reference': face_ref}
        state = run_gen_one(job_dir, shot_id, parts['positive_tags'],
                            parts['framing'], config, client, negative_prompt=shot_config['negative_prompt'],
                            generation_reference=relation,
                            repair_diagnosis=(repair_diagnoses or {}).get(str(entry['number'])))
        latest = next(a for a in reversed(state["attempts"])
                      if a["shot_id"] == f'shot{entry["number"]:02d}')
        if latest["status"] != "completed":
            break
    if state is not None:
        write_story_manifest(job_dir, config, client)
        if all(any(a["status"] == "completed" and a["shot_id"] == f'shot{entry["number"]:02d}'
                   for a in state["attempts"]) for entry in board["shots"]):
            make_contact_sheet(job_dir, review_dir)
    return state or {}
