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


def resolve_job(job_dir: str | Path, *, work=None, character_tag=None) -> dict:
    job_dir = Path(job_dir)
    path = job_dir / "identity.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not (work or character_tag):
            return existing
        if existing.get("status") == "locked":
            raise ValueError("identity already locked; use a new job for a different selection")
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    options = {k: v for k, v in {"work": work or job.get('generation_options',{}).get('work'), "character_tag": character_tag or job.get('generation_options',{}).get('character_tag')}.items() if v}
    identity = resolve_identity(job["character"], job_dir / "cache" / "danbooru", **options)
    save_json_atomic(path, identity)
    return identity


def plan_job(job_dir: str | Path, config: dict) -> dict:
    job_dir = Path(job_dir)
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    identity = resolve_job(job_dir)
    if identity["status"] != "locked":
        raise ValueError("identity requires clarification before planning")
    return plan_story(job_dir, job["story"], identity, job["shot_count"],
                      config["model_service_url"], max_calls=config.get("planning", {}).get("max_calls", 5),
                      retries=config.get("planning", {}).get("retries", config.get("planning", {}).get("max_calls", 5)), config=config)


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
    for item in config["style_loras"] + ([config["character_lora"]] if config.get("character_lora") else []):
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
        parts = assemble_prompt(identity, entry, config=config,
                                **({'props': board['props']} if board.get('props') else {}))
        workflow = attempt["workflow"]
        images.append({"shot_id": shot_id, "attempt_number": attempt["attempt_number"],
                       "prompt_id": attempt["prompt_id"], "filename_prefix": attempt["filename_prefix"],
                       "identity_block": parts["identity_block"], "prompt_parts": parts,
                       "positive_prompt": workflow["4"]["inputs"].get("text",workflow["4"]["inputs"].get("prompt")),
                       "negative_prompt": workflow["5"]["inputs"].get("text",workflow["5"]["inputs"].get("prompt")),
                       "seed": attempt["seed"], "sampler": workflow["7"]["inputs"],
                       "resolution": workflow["6"]["inputs"], "reference": attempt.get("reference"),
                       "created_at": attempt["created_at"], "completed_at": attempt["completed_at"], **attempt["image"]})
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["created_at"])).total_seconds()
    prior_path = job_dir / "manifest.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    model_path=job_dir/"models_snapshot.json"
    models=json.loads(model_path.read_text()) if model_path.exists() else _model_manifest(config,client)
    if not model_path.exists():save_json_atomic(model_path,models)
    manifest = {"job_id": job["job_id"], "character_input": job["character"],
                "story": job["story"], "identity_file": "identity.json",
                "storyboard_file": "storyboard.json", "models": models,
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
        chosen = attempts[-1]
        if (job_dir/"cycle.json").exists():
            selected = json.loads((job_dir/"cycle.json").read_text()).get("shots", {}).get(str(entry["number"]), {}).get("best_attempt")
            chosen = next((a for a in attempts if a["attempt_number"]==selected), chosen)
        path = job_dir / chosen["image"]["path"]
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


def tier_recheck_eligible(identity, config):
    return (bool(config.get('tier_cache')) and identity.get('status')=='needs_user'
            and identity.get('identity_tier')=='L4' and bool(identity.get('character_tag')))


def run_story_job(job_dir: str | Path, config: dict, client,
                  *, review_dir: str | Path = "review", repair_diagnoses: dict | None = None) -> dict:
    job_dir = Path(job_dir)
    if (job_dir/'cycle.json').exists():
        from storycg.runtime_gate import run_repair_cycle
        return run_repair_cycle(job_dir, load_state(job_dir)['config'], client)
    identity = resolve_job(job_dir)
    recheck_tier = tier_recheck_eligible(identity, config)
    if identity.get("status")=="needs_user" and not recheck_tier:
        return {"job_id":job_dir.name,"status":"needs_user","needs_user":True,"reason":identity["reason"],"question":identity["question"],"attempts":[],"tier_evidence":identity.get("tier_evidence",{})}
    if identity["status"] != "locked" and not recheck_tier:
        raise ValueError("identity requires clarification before generation")
    if config.get("repair_cycle_enabled"):
        from storycg.budget import Budget
        from storycg.cycle import settings
        gate=Budget(job_dir,settings())
        gate.check()
    from storycg.tier_gate import ensure_identity_tier
    from storycg.tiers import tier_generation_config
    if config.get("repair_cycle_enabled"):
        from storycg.budget import CycleClient
        tier_vlm=CycleClient(job_dir.resolve(),config["model_service_url"],budget=gate,max_calls=24,max_seconds=gate.remaining_seconds())
        try:identity=ensure_identity_tier(job_dir,identity,config,client,vlm=tier_vlm)
        finally:tier_vlm.close()
    else:
        identity = ensure_identity_tier(job_dir, identity, config, client)
    if identity.get('identity_tier') not in ('L1', 'L2', 'L3'):
        return {'job_id': job_dir.name, 'status': 'needs_user' if identity.get('identity_tier')=='L4' else identity.get('identity_tier'),
                'needs_user':identity.get('identity_tier')=='L4','reason':identity.get('reason'),'question':identity.get('question'),
                'attempts': [], 'tier_evidence': identity.get('tier_evidence', {})}
    config = tier_generation_config(config, identity)
    from storycg.reference import ensure_portrait
    from storycg.portrait_gate import recover_d7_portrait
    recovered_portrait=recover_d7_portrait(job_dir,identity,config)
    if config.get("repair_cycle_enabled"):gate.check()
    if config.get("repair_cycle_enabled"):
        portrait_vlm=CycleClient(job_dir.resolve(),config["model_service_url"],budget=gate,max_calls=6,max_seconds=gate.remaining_seconds())
        try:
            try:portrait=recovered_portrait or ensure_portrait(job_dir,identity,config,client,vlm=portrait_vlm)
            except ValueError:
                portrait=recover_d7_portrait(job_dir,identity,config)
                if portrait is None:raise
        finally:portrait_vlm.close()
    else:
        portrait = ensure_portrait(job_dir, identity, config, client)
    if config.get("repair_cycle_enabled"):gate.check()
    board = plan_job(job_dir, config)
    if (job_dir / "state.json").exists():
        config = load_state(job_dir)["config"]
    else:
        config = copy.deepcopy(config)
        if config.get("repair_cycle_enabled"):
            config["budget"]["max_images_per_job"] -= gate.usage()["submission_attempts"]
        config["budget"]["max_job_minutes"] = min(60, config["budget"]["max_job_minutes"])
    config = tier_generation_config(config, identity)
    from storycg.controls import apply_options, portrait_reference, upload_reference
    from storycg.reference import edit_instruction
    job = json.loads((job_dir / 'job.json').read_text())
    config = tier_generation_config(apply_options(config, job.get('generation_options')), identity)
    config.pop('generation_reference', None)
    config['auto_retry_failed'] = False
    control = config['identity_control']
    face_ref = (portrait_reference(job_dir, portrait, 'face', client, 'ipadapter', control['ipadapter_strength'])
                if control['mechanism'] == 'ipadapter' else None)
    full_ref = (portrait_reference(job_dir, portrait, 'image', client, 'qwen_edit', 1.0)
                if config['layout_mode'] == 'qwen' else None)
    state = None
    for entry in board["shots"]:
        if config.get("repair_cycle_enabled"):gate.check()
        parts = assemble_prompt(identity, entry, config=config,
                                **({'props': board['props']} if board.get('props') else {}))
        shot_config = copy.deepcopy(config)
        shot_config["negative_prompt"] = ", ".join(filter(None, [config["negative_prompt"], parts["negative_tags"]]))
        shot_id = f'shot{entry["number"]:02d}'
        relation = face_ref
        if full_ref:
            layout_id = f'layout{entry["number"]:02d}'
            state = run_gen_one(job_dir, layout_id, edit_instruction(entry, board.get('props', [])),
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
    if config.get("repair_cycle_enabled") and state is not None and (job_dir/"contact_sheet.jpg").exists():
        from storycg.runtime_gate import run_repair_cycle
        return run_repair_cycle(job_dir,config,client)
    return state or {}


def resolve_and_validate_job(job_dir,config):
    from storycg.comfy import ComfyClient
    from storycg.tier_gate import ensure_identity_tier
    identity=resolve_job(job_dir)
    if identity.get('status')!='locked' and not tier_recheck_eligible(identity,config):return identity
    client=ComfyClient(config['comfyui_url'],config['http_timeout_seconds'])
    try:return ensure_identity_tier(job_dir,identity,config,client)
    finally:client.close()
