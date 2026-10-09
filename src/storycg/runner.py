"""Submit once, reconcile after interruption, and persist downloaded output."""

import fcntl
import hashlib
import io
import os
import secrets
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image

from storycg.reconcile import Finding, reconcile_attempt
from storycg.state import load_state, make_prefix, save_json_atomic, save_state
from storycg.workflow import build_workflow


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _submission_count(state: dict) -> int:
    return sum(a.get("post_count", int("submission_started_at" in a))
               for a in state["attempts"])


@contextmanager
def _job_lock(job_dir: Path):
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / ".lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _save(job_dir: Path, state: dict) -> None:
    state["updated_at"] = _now()
    state["stats"] = {
        "generated_images": sum(a["status"] == "completed" for a in state["attempts"]),
        "review_calls": 0,
        "submission_attempts": _submission_count(state),
        "elapsed_seconds": round((datetime.now(timezone.utc) -
                                  datetime.fromisoformat(state["created_at"])).total_seconds(), 1),
    }
    save_state(job_dir, state)


def _write_manifest(job_dir: Path, state: dict) -> None:
    images = []
    for attempt in state["attempts"]:
        if attempt["status"] == "completed":
            images.append({
                "shot_id": attempt["shot_id"], "attempt_number": attempt["attempt_number"],
                "prompt_id": attempt["prompt_id"], "filename_prefix": attempt["filename_prefix"],
                "seed": attempt["seed"], "reference": attempt.get("reference"),
                "positive_prompt": attempt["workflow"]["4"]["inputs"].get("text", attempt["workflow"]["4"]["inputs"].get("prompt")),
                "negative_prompt": attempt["workflow"]["5"]["inputs"].get("text", attempt["workflow"]["5"]["inputs"].get("prompt")),
                **attempt["image"],
            })
    save_json_atomic(job_dir / "manifest.json", {"job_id": state["job_id"], "images": images,
                                                   "style": state["config"].get("style_snapshot"),
                                                   "config": state["config"], "usage": state.get("stats", {})})


def _download_completed(job_dir: Path, state: dict, attempt: dict, client, history: dict) -> None:
    output_nodes = [key for key, node in attempt["workflow"].items()
                    if node["class_type"] == "SaveImage"]
    images = [image for key in output_nodes
              for image in history.get("outputs", {}).get(key, {}).get("images", [])]
    if len(images) != 1:
        raise ValueError(f"expected one SaveImage output, got {len(images)}")
    reference = images[0]
    # ComfyUI splits a directory-bearing prefix into subfolder + base filename.
    remote_path = "/".join(part for part in (reference.get("subfolder", ""), reference["filename"]) if part)
    if not remote_path.startswith(attempt["filename_prefix"]):
        raise ValueError("ComfyUI output filename does not match this attempt")
    image_bytes = client.download_image(reference)
    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.format != "PNG":
            raise ValueError("ComfyUI output is not PNG")
        image.verify()
    with Image.open(io.BytesIO(image_bytes)) as image:
        width, height = image.size
    # Keep job-local artefacts flat; only the remote prefix carries the root directory.
    path = job_dir / f'{PurePosixPath(attempt["filename_prefix"]).name}.png'
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=job_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(image_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    attempt["image"] = {
        "path": path.name, "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "width": width, "height": height, "comfy_output": reference,
    }
    attempt["status"] = "completed"
    attempt["completed_at"] = _now()
    _save(job_dir, state)
    _write_manifest(job_dir, state)


def _finish_history(job_dir: Path, state: dict, attempt: dict, client, record: dict) -> None:
    status = record.get("status") or {}
    if status.get("status_str") == "error" or status.get("completed") is False:
        attempt["status"] = "failed"
        attempt["error"] = status
        _save(job_dir, state)
    elif status.get("status_str") == "success" and status.get("completed") is True:
        _download_completed(job_dir, state, attempt, client, record)
    else:
        attempt["status"] = "uncertain"
        attempt["error"] = "history record has no final success or failure status"
        _save(job_dir, state)


def _process(job_dir: Path, state: dict, attempt: dict, client, *, recovering: bool) -> None:
    config = state["config"]
    if recovering:
        queue = client.get_queue()
        history = (client.get_history(attempt["prompt_id"]) if attempt["prompt_id"]
                   else client.get_all_history())
        finding = reconcile_attempt(attempt, queue, history)
        if (finding.kind == "uncertain" and not attempt["prompt_id"] and
                attempt["status"] in ("submitting", "uncertain")):
            queue = client.get_queue()
            history = client.get_all_history()
            finding = reconcile_attempt(attempt, queue, history)
            if finding.kind == "uncertain":
                finding = Finding("submit")
    else:
        finding = Finding("submit")

    if finding.kind == "complete":
        attempt["prompt_id"] = finding.prompt_id
        _download_completed(job_dir, state, attempt, client, finding.history)
        return
    if finding.kind == "failed":
        attempt["prompt_id"] = finding.prompt_id
        _finish_history(job_dir, state, attempt, client, finding.history)
        return
    if finding.kind in ("uncertain", "conflict"):
        attempt["status"] = finding.kind
        attempt["error"] = ("submission outcome cannot be proven; no automatic repost"
                            if finding.kind == "uncertain" else "multiple prompt IDs share this prefix")
        _save(job_dir, state)
        return
    if finding.kind == "submit":
        if _submission_count(state) >= config["budget"]["max_images_per_job"]:
            raise ValueError("job submission budget exhausted")
        attempt["status"] = "submitting"
        attempt["submission_started_at"] = _now()
        attempt["post_count"] = attempt.get("post_count", int("prompt_id" in attempt and
                                                               attempt.get("prompt_id") is not None)) + 1
        _save(job_dir, state)
        try:
            prompt_id = client.submit_prompt(attempt["workflow"], attempt["client_id"])
        except Exception as exc:
            attempt["status"] = "uncertain"
            attempt["error"] = f"POST outcome unknown: {type(exc).__name__}: {exc}"
            _save(job_dir, state)
            raise
        attempt["prompt_id"] = prompt_id
        attempt["status"] = "submitted"
        attempt.pop("error", None)
        _save(job_dir, state)
    else:
        attempt["prompt_id"] = finding.prompt_id
        attempt["status"] = "queued"
        _save(job_dir, state)

    record = client.wait_for_history(
        attempt["prompt_id"], config["poll_interval_seconds"],
        config["budget"]["max_job_minutes"] * 60,
    )
    if record is None:
        attempt["status"] = "waiting"
        _save(job_dir, state)
    else:
        _finish_history(job_dir, state, attempt, client, record)


def _new_attempt(job_dir: Path, state: dict, shot_id: str, positive_tags: str,
                 framing: str, negative_prompt: str | None = None, *, generation_reference: dict | None = None,
                 dropped_scene_tags: list | None = None, indoor_negative: dict | None = None) -> dict:
    if len(state["attempts"]) >= state["config"]["budget"]["max_images_per_job"]:
        raise ValueError("job image budget exhausted")
    if _submission_count(state) >= state["config"]["budget"]["max_images_per_job"]:
        raise ValueError("job submission budget exhausted")
    attempt_number = 1 + max((a["attempt_number"] for a in state["attempts"]
                              if a["shot_id"] == shot_id), default=0)
    prefix = make_prefix(state["config"].get("prefix_namespace", state["job_id"]), shot_id, attempt_number,
                         root=state["config"].get("comfy_prefix_root", ""))
    seed = state["config"].get("generation_seed")
    if seed is None:
        seed = secrets.randbits(63)
    if not isinstance(seed, int) or not 0 <= seed < 2**64:
        raise ValueError("generation_seed must be an unsigned 64-bit integer")
    workflow_config = dict(state["config"])
    if negative_prompt is not None:
        workflow_config["negative_prompt"] = negative_prompt
    if generation_reference is not None:
        workflow_config["generation_reference"] = generation_reference
    attempt = {
        "job_id": state["job_id"], "shot_id": shot_id, "attempt_number": attempt_number,
        "seed": seed, "positive_tags": positive_tags, "framing": framing,
        "filename_prefix": prefix, "client_id": uuid.uuid4().hex,
        "prompt_id": None, "status": "ready", "post_count": 0,
        "workflow": build_workflow(workflow_config, positive_tags, framing, seed, prefix),
        "created_at": _now(), "reference": workflow_config.get("generation_reference"),
        # Scene tags dropped by the framing object cap travel with the attempt (V2-09).
        "dropped_scene_tags": list(dropped_scene_tags or []),
        # V2-18 (D60): the indoor-precipitation negatives added to this shot, and the tags skipped with a reason.
        "indoor_precipitation_negative": indoor_negative,
    }
    state["attempts"].append(attempt)
    _save(job_dir, state)
    return attempt


def _retry_if_failed(job_dir: Path, state: dict, attempt: dict, client) -> None:
    if attempt["status"] == "failed" and state["config"].get("auto_retry_failed", True):
        replacement = _new_attempt(job_dir, state, attempt["shot_id"],
                                   attempt["positive_tags"], attempt["framing"],
                                   attempt["workflow"]["5"]["inputs"].get("text", attempt["workflow"]["5"]["inputs"].get("prompt")), generation_reference=attempt.get("reference"),
                                   dropped_scene_tags=attempt.get("dropped_scene_tags"),
                                   indoor_negative=attempt.get("indoor_precipitation_negative"))
        _process(job_dir, state, replacement, client, recovering=False)


def run_gen_one(job_dir: str | Path, shot_id: str, positive_tags: str, framing: str,
                config: dict, client, *, negative_prompt: str | None = None,
                repair_diagnosis: dict | None = None, generation_reference: dict | None = None,
                dropped_scene_tags: list | None = None, indoor_negative: dict | None = None) -> dict:
    job_dir = Path(job_dir)
    job_id = job_dir.name
    make_prefix(job_id, shot_id, 1, root=config.get("comfy_prefix_root", ""))
    with _job_lock(job_dir):
        state_path = job_dir / "state.json"
        if state_path.exists():
            state = load_state(job_dir)
            if state["job_id"] != job_id:
                raise ValueError("job directory and saved job ID differ")
        else:
            state = {"schema_version": 1, "job_id": job_id, "config": config,
                     "created_at": _now(), "attempts": []}
        matching = [attempt for attempt in state["attempts"] if attempt["shot_id"] == shot_id]
        if matching:
            attempt = matching[-1]
            if generation_reference is not None and generation_reference != attempt.get("reference") and repair_diagnosis is None:
                raise ValueError("shot already exists with different generation reference; diagnosis required")
            if repair_diagnosis is not None and attempt['status'] == 'completed':
                if not all(repair_diagnosis.get(k) for k in ('diagnosis', 'hypothesis', 'changes')):
                    raise ValueError("repair needs diagnosis, hypothesis and changes")
                old_negative = attempt['workflow']['5']['inputs'].get('text', attempt['workflow']['5']['inputs'].get('prompt'))
                new_negative = negative_prompt if negative_prompt is not None else old_negative
                same_conditioning = (attempt['positive_tags'], attempt['framing'], old_negative) == (positive_tags, framing, new_negative) and (generation_reference is None or generation_reference == attempt.get("reference"))
                if same_conditioning and attempt.get('repair_diagnosis') == repair_diagnosis:
                    return state  # A completed diagnosed repair is idempotent.
                if same_conditioning:
                    raise ValueError("repair must change conditioning, not seed alone")
                replacement = _new_attempt(job_dir, state, shot_id, positive_tags, framing, new_negative, generation_reference=generation_reference, dropped_scene_tags=dropped_scene_tags, indoor_negative=indoor_negative)
                replacement['repair_diagnosis'] = repair_diagnosis
                _save(job_dir, state)
                _process(job_dir, state, replacement, client, recovering=False)
                return state
            if negative_prompt is not None and attempt["workflow"]["5"]["inputs"].get("text", attempt["workflow"]["5"]["inputs"].get("prompt")) != negative_prompt:
                raise ValueError("shot already exists with different negative prompt")
            if attempt["positive_tags"] != positive_tags or attempt["framing"] != framing:
                raise ValueError("shot already exists with different tags or framing")
            if attempt["status"] not in ("completed", "failed"):
                _process(job_dir, state, attempt, client, recovering=True)
            _retry_if_failed(job_dir, state, attempt, client)
            return state
        attempt = _new_attempt(job_dir, state, shot_id, positive_tags, framing, negative_prompt, generation_reference=generation_reference, dropped_scene_tags=dropped_scene_tags, indoor_negative=indoor_negative)
        _process(job_dir, state, attempt, client, recovering=False)
        _retry_if_failed(job_dir, state, attempt, client)
        return state


def resume_job(job_dir: str | Path, client) -> dict:
    job_dir = Path(job_dir)
    with _job_lock(job_dir):
        state = load_state(job_dir)
        if state["job_id"] != job_dir.name:
            raise ValueError("job directory and saved job ID differ")
        shot_ids = list(dict.fromkeys(a["shot_id"] for a in state["attempts"]))
        for shot_id in shot_ids:
            attempt = next(a for a in reversed(state["attempts"]) if a["shot_id"] == shot_id)
            if attempt["status"] not in ("completed", "failed"):
                _process(job_dir, state, attempt, client, recovering=True)
            _retry_if_failed(job_dir, state, attempt, client)
        return state
