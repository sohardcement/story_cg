#!/usr/bin/env python3
"""Build a read-only job evidence summary and an uncropped comparison JPEG.

This tool uses persisted reviews and submission records. It neither calls services
nor selects alternative images or changes a review conclusion.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont, ImageOps

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))
OUTFIT_KEY = "outfit_detail_vs_portrait"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(root, relative, errors, sources, *, required=False):
    path = root / relative
    if not path.exists():
        if required:
            errors.append(f"missing: {relative}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sources[str(relative)] = sha256(path)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except (OSError, ValueError) as exc:
        errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        return {}


def image_record(root, record):
    """Check the recorded file without substituting another candidate."""
    result = dict(record or {})
    relative = result.get("path")
    if not relative:
        return {**result, "exists": False, "sha256_matches": None}
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        return {**result, "exists": False, "error": "image path escapes job directory"}
    result["exists"] = path.is_file()
    if result["exists"]:
        result["actual_sha256"] = sha256(path)
        result["sha256_matches"] = (
            result["actual_sha256"] == result["sha256"]
            if result.get("sha256") else None
        )
    else:
        result["sha256_matches"] = None
    return result


def outfit_findings(value, path="$"):
    """Locate the exact persisted item, including nested v3 audit records."""
    records = []
    if isinstance(value, dict):
        item = value.get(OUTFIT_KEY)
        if isinstance(item, dict) and "status" in item:
            records.append({"json_path": f"{path}.{OUTFIT_KEY}", **item})
        for key, item in value.items():
            if key != OUTFIT_KEY:
                records.extend(outfit_findings(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            records.extend(outfit_findings(item, f"{path}[{index}]"))
    return records


def shot_final_records(final_review, number):
    return {
        key: [item for item in final_review.get(key, [])
              if str(item.get("shot")) == str(number)]
        for key in ("single", "consistency", "faces", "detection_items")
        if isinstance(final_review.get(key), list)
    }


def submission_summary(root, errors, sources):
    records = []
    for path in sorted(root.rglob("state.json")):
        relative = path.relative_to(root)
        state = read_json(root, relative, errors, sources)
        for index, attempt in enumerate(state.get("attempts", [])):
            if attempt.get("inherited"):
                continue
            records.append({
                "state_file": str(relative), "attempt_index": index,
                **{key: attempt.get(key) for key in (
                    "job_id", "shot_id", "attempt_number", "status", "post_count",
                    "prompt_id", "filename_prefix", "repair_key", "repair_stage",
                )},
                "image": image_record(path.parent, attempt.get("image")),
            })
    prompt_ids = defaultdict(list)
    prefixes = defaultdict(list)
    for record in records:
        address = {key: record[key] for key in (
            "state_file", "attempt_index", "shot_id", "attempt_number", "status",
        )}
        if record.get("prompt_id"):
            prompt_ids[record["prompt_id"]].append(address)
        if record.get("filename_prefix") and record.get("post_count", 0):
            prefixes[record["filename_prefix"]].append(address)
    duplicates = {key: value for key, value in prompt_ids.items() if len(value) > 1}
    multiple_posts = [record for record in records if (record.get("post_count") or 0) > 1]
    unknown_ids = [record for record in records
                   if (record.get("post_count") or 0) > 0 and not record.get("prompt_id")]
    return {
        "method": "persisted non-inherited state attempts; no server history query",
        "attempts": records,
        "recorded_submission_attempts": sum(record.get("post_count") or 0 for record in records),
        "unique_prompt_id_count": len(prompt_ids),
        "duplicate_prompt_ids": duplicates,
        "duplicate_submitted_prefixes": {key: value for key, value in prefixes.items() if len(value) > 1},
        "multiple_posts_per_attempt": multiple_posts,
        "submitted_without_prompt_id": unknown_ids,
        "unique_check": "fail" if duplicates or multiple_posts else ("incomplete" if unknown_ids else "pass"),
    }


def planning_records(log):
    calls = []
    for index, call in enumerate(log.get("calls", [])):
        events = call.get("tag_validation", [])
        action_events = [event for event in events if
                         ".shot.tags" in str(event.get("field", "")) or
                         ".shot.pose" in str(event.get("field", "")) or
                         event.get("pose_field") or
                         "pose" in str(event.get("action", "")) or
                         "action" in str(event.get("field", ""))]
        omissions = [event for event in events if any(word in str(event.get("action", ""))
                    for word in ("omit", "discard", "drop", "reject"))]
        calls.append({
            "call_index": index,
            **{key: call.get(key) for key in ("status", "error", "rule_id", "planning_contract")},
            "action_tag_events": action_events, "omission_events": omissions,
            "pose_anchors": call.get("pose_anchors", []),
        })
    return calls


def summarize_job(directory):
    root = Path(directory).resolve()
    errors, sources = [], {}
    load = lambda name, required=False: read_json(root, name, errors, sources, required=required)
    cycle = load("cycle.json", True)
    board = load("storyboard.json", True)
    initial_board = load("initial_storyboard.json")
    plan = load("plan_log.json", True)
    state = load("state.json", True)
    manifest = load("manifest.json", True)
    final_review = cycle.get("final_review") or load("review.json")
    execution = load("execution.json")
    agent = load("agent.json")
    submissions = submission_summary(root, errors, sources)
    final_images = [image_record(root, image) for image in manifest.get("images", [])]
    try:
        from storycg.budget import count_usage
        usage = count_usage(root)
        usage_error = None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        usage, usage_error = None, f"{type(exc).__name__}: {exc}"
        errors.append("count_usage: " + usage_error)
    limits = {key: cycle.get("config", {}).get(key) for key in (
        "max_images", "max_vision_calls", "max_llm_calls", "max_minutes",
    )}
    selected = {image.get("shot_id"): image for image in final_images}
    entries = {str(entry["number"]): entry for entry in board.get("shots", [])}
    initial_entries = {str(entry["number"]): entry for entry in initial_board.get("shots", [])}
    shots = []
    for number in sorted(set(entries) | set(cycle.get("shots", {})), key=lambda value: int(value)):
        slot = cycle.get("shots", {}).get(number, {})
        initial_file = f"cycle_artifacts/initial_{number}.json"
        initial_audit = load(initial_file)
        history = []
        for event in slot.get("history", []):
            decision = event.get("decision", {})
            source = next((attempt.get("image", {}) for attempt in state.get("attempts", [])
                           if attempt.get("shot_id") == f"shot{int(number):02d}"
                           and attempt.get("attempt_number") == event.get("source_attempt")), {})
            source_image = image_record(root, source)
            history.append({
                "round": event.get("round"), "status": event.get("status"),
                **{key: decision.get(key) for key in (
                    "diagnosis", "hypothesis", "changes", "primitive", "problem", "edit", "shot",
                )},
                "source_attempt": event.get("source_attempt"),
                "source_sha256": event.get("source_sha256"),
                "source_image": source_image,
                "source_sha256_matches_actual": (
                    event["source_sha256"] == source_image.get("actual_sha256")
                    if event.get("source_sha256") and source_image.get("exists") else None
                ),
                "output": image_record(root, event.get("output")),
                "result": event.get("result"),
                "outfit_findings_after": outfit_findings(event.get("audit", {})),
            })
        initial_findings = outfit_findings(initial_audit)
        best_findings = outfit_findings(slot.get("best_audit", {}))
        final_findings = outfit_findings(shot_final_records(final_review, number))
        failed = any(item.get("status") == "fail"
                     for item in initial_findings + best_findings + final_findings)
        outfit_repairs = [event for event in history
                          if event.get("problem") in ("outfit", OUTFIT_KEY)]
        # A prepared diagnosis is not an executed image repair.
        executed = [event for event in outfit_repairs if event["output"].get("exists")]
        entry = slot.get("entry") or entries.get(number, {})
        action = entry.get("shot", {})
        initial_action = initial_entries.get(number, {}).get("shot", {})
        shots.append({
            "shot": int(number), "best_attempt": slot.get("best_attempt"),
            "stop": slot.get("stop"), "diagnosis_error": slot.get("diagnosis_error"),
            "selected_image": selected.get(f"shot{int(number):02d}"),
            "initial_outfit_detail": {"source": initial_file, "available": bool(initial_findings), "findings": initial_findings},
            "current_outfit_detail": {"source": "cycle.shots.best_audit", "available": bool(best_findings), "findings": best_findings},
            "final_outfit_detail": {"source": "cycle.final_review or review.json", "available": bool(final_findings), "findings": final_findings},
            "outfit_fail_observed": failed,
            "outfit_repair_executed": bool(executed),
            "outfit_fail_without_executed_repair": failed and not executed,
            "outfit_repair_rounds": [event["round"] for event in executed],
            "repair_history": history,
            "action": {key: action.get(key) for key in (
                "pose", "action", "description", "expression", "spatial_relation", "change_from_previous", "tags",
            )},
            "initial_action": {key: initial_action.get(key) for key in (
                "pose", "action", "spatial_relation", "change_from_previous", "tags",
            )},
            "submitted_positive_tags": [{
                "attempt_number": attempt.get("attempt_number"),
                "positive_tags": attempt.get("positive_tags"),
                "prompt_id": attempt.get("prompt_id"),
            } for attempt in state.get("attempts", []) if attempt.get("shot_id") == f"shot{int(number):02d}"],
        })
    incomplete = list(errors)
    if cycle.get("phase") != "complete":
        incomplete.append("cycle has not reached complete")
    if not final_review:
        incomplete.append("final review is absent")
    expected_ids = {f"shot{int(number):02d}" for number in entries}
    if not entries or len(final_images) != len(entries) or set(selected) != expected_ids:
        incomplete.append("manifest does not contain one selected image per storyboard shot")
    if any(not image.get("exists") or image.get("sha256_matches") is not True for image in final_images):
        incomplete.append("a selected image is missing or its recorded sha256 is absent/mismatched")
    sheet = root / "contact_sheet.jpg"
    if not sheet.is_file():
        incomplete.append("final contact_sheet.jpg is absent")
    if not (root / "result.md").is_file():
        incomplete.append("final result.md is absent")
    return {
        "job_directory": str(root), "job_id": state.get("job_id") or manifest.get("job_id"),
        "status": "incomplete" if incomplete else "complete",
        "status_meaning": "artifact completeness only; original review conclusions remain unchanged",
        "incomplete_reasons": incomplete, "read_errors": errors,
        "cycle_phase": cycle.get("phase"), "cycle_blocked": cycle.get("blocked"),
        "execution_phase": execution.get("phase"), "agent_phase": agent.get("phase"),
        "final_review_conclusion": final_review.get("conclusion"),
        "review_gate": manifest.get("review_gate"),
        "usage": usage, "usage_method": "storycg.budget.count_usage (offline)",
        "usage_error": usage_error, "persisted_budget_limits": limits,
        "manifest_usage": manifest.get("usage"), "submissions": submissions,
        "shots": shots, "planning_calls": planning_records(plan),
        "final_images": final_images,
        "contact_sheet": {"path": str(sheet), "exists": sheet.is_file(),
                          "sha256": sha256(sheet) if sheet.is_file() else None},
        "source_json_sha256": sources,
    }


def comparison_image(before, after):
    """Equal-height whole-image scaling; no cropping, padding inside, or picking."""
    height = max(before.height, after.height)
    panels = [image.convert("RGB").resize(
        (max(1, round(image.width * height / image.height)), height), Image.Resampling.LANCZOS,
    ) for image in (before, after)]
    gutter, header = 24, max(64, round(height * 0.05))
    canvas = Image.new("RGB", (panels[0].width + panels[1].width + gutter, height + header), "white")
    draw = ImageDraw.Draw(canvas)
    left = 0
    for label, panel in zip(("Before", "After"), panels):
        font = ImageFont.load_default(size=max(10, min(40, header // 2, panel.width // 4)))
        draw.text((left + panel.width / 2, header / 2), label, fill="black", anchor="mm", font=font)
        canvas.paste(panel, (left, header))
        left += panel.width + gutter
    return canvas


def validate_destination(path, before, after):
    path = Path(path).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError("outputs must stay inside this workspace")
    if any(path.is_relative_to(root) for root in (before, after)):
        raise ValueError("outputs must not modify either source job")
    if path.relative_to(WORKSPACE).parts[0] in ("verify", "orders", ".git", ".venv", ".agents", ".codex"):
        raise ValueError("output is inside a protected directory")
    if path.name in ("TASK.md", "SUPERVISOR.md", "PROGRESS.md", "AGENTS.md"):
        raise ValueError("output would replace a protected file")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="comparison JPEG")
    parser.add_argument("--evidence", type=Path, required=True, help="offline evidence JSON")
    args = parser.parse_args(argv)
    before, after = args.before.resolve(), args.after.resolve()
    try:
        output = validate_destination(args.output, before, after)
        evidence_path = validate_destination(args.evidence, before, after)
        if output == evidence_path:
            raise ValueError("JPEG and JSON destinations must differ")
    except ValueError as exc:
        parser.error(str(exc))
    evidence = {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
                "before": summarize_job(before), "after": summarize_job(after)}
    evidence["status"] = "complete" if all(evidence[key]["status"] == "complete" for key in ("before", "after")) else "incomplete"
    evidence["comparison"] = {"output": str(output), "status": "not_written",
                              "left": "before/contact_sheet.jpg", "right": "after/contact_sheet.jpg",
                              "existing_output_preserved": output.exists()}
    if evidence["status"] == "complete":
        try:
            with Image.open(before / "contact_sheet.jpg") as old, Image.open(after / "contact_sheet.jpg") as new:
                canvas = comparison_image(ImageOps.exif_transpose(old), ImageOps.exif_transpose(new))
            output.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(output, format="JPEG", quality=95, subsampling=0)
            evidence["comparison"].update(status="written", width=canvas.width,
                                          height=canvas.height, sha256=sha256(output),
                                          existing_output_preserved=False)
        except (OSError, ValueError) as exc:
            evidence["status"] = "incomplete"
            evidence["comparison"]["error"] = f"{type(exc).__name__}: {exc}"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": evidence["status"], "evidence": str(evidence_path),
                      "comparison": evidence["comparison"]}, ensure_ascii=False))
    return 0 if evidence["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
