#!/usr/bin/env python3
"""Replay candidate selection from persisted reviews without running an audit.

Only the requested evidence JSON is written. Job data, images, review statuses,
and calibration records remain untouched. No detector or service is invoked.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = WORKSPACE / "reports" / "assets" / "P10-05"
sys.path.insert(0, str(WORKSPACE / "src"))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path, sources: dict[str, str]):
    sources[str(path)] = sha256(path)
    return json.loads(path.read_text(encoding="utf-8"))


def image_mapping(root: Path, audit: dict, state: dict, sources: dict) -> dict:
    recorded = audit["image"]
    path = (root / recorded["path"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("persisted image path escapes the job directory")
    actual_sha = sha256(path)
    sources[str(path)] = actual_sha
    if actual_sha != recorded["sha256"]:
        raise ValueError("persisted image SHA does not match the original file")
    matches = [
        attempt for attempt in state["attempts"]
        if attempt.get("status") == "completed"
        and attempt.get("image", {}).get("path") == recorded["path"]
        and attempt.get("image", {}).get("sha256") == recorded["sha256"]
    ]
    if len(matches) != 1:
        raise ValueError("the persisted image does not map to one completed attempt")
    attempt = matches[0]
    return {
        "path": recorded["path"],
        "recorded_sha256": recorded["sha256"],
        "actual_sha256": actual_sha,
        "sha256_matches": True,
        "shot_id": attempt["shot_id"],
        "attempt_number": attempt["attempt_number"],
        "audit_conclusion": audit["conclusion"],
    }


def findings(audit: dict, neighbors: list, audit_file: str, neighbors_file: str) -> list:
    """Copy each direct saved item once; nested v3 duplicates are not metrics."""
    layers = [
        (audit_file, "$.single", audit["single"]),
        (audit_file, "$.consistency", audit["consistency"]),
        (audit_file, "$.detection_items.review", audit["detection_items"]["review"]),
    ]
    layers.extend((neighbors_file, f"$[{index}]", row) for index, row in enumerate(neighbors))
    records = []
    for source, pointer, result in layers:
        for key, item in result.get("data", {}).items():
            if isinstance(item, dict):
                records.append({
                    "source": source,
                    "json_path": f"{pointer}.data.{key}",
                    "audit_status": result.get("status"),
                    "key": key,
                    "item": item,
                })
    return records


def replay_module(module, before_audit, after_audit, before_neighbors, after_neighbors,
                  before_mapping, after_mapping) -> dict:
    # These helpers consume saved dictionaries only. Do not call audit_candidate,
    # audit_detection, review_job, run_repair_cycle, or any remote client.
    before = [*module.comparison_findings(before_audit), *before_neighbors]
    after = [*module.comparison_findings(after_audit), *after_neighbors]
    before_vector = tuple(module.reliable_failure_vector(before))
    after_vector = tuple(module.reliable_failure_vector(after))
    improved = module.candidate_improved(before, after)
    decisive_index = next(
        (index for index, pair in enumerate(zip(before_vector, after_vector))
         if pair[0] != pair[1]), None
    )
    return {
        "reliable_failure_vector_before": before_vector,
        "reliable_failure_vector_after": after_vector,
        "reliable_fails_before_including_detection": module.reliable_failures(before),
        "reliable_fails_after_including_detection": module.reliable_failures(after),
        "decisive_tier_index": decisive_index,
        "candidate_improved": improved,
        "would_select_attempt": (
            after_mapping["attempt_number"] if improved else before_mapping["attempt_number"]
        ),
        "would_select_image": after_mapping["path"] if improved else before_mapping["path"],
        "selected_saved_audit_conclusion": (
            after_audit["conclusion"] if improved else before_audit["conclusion"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--shot", type=int, required=True)
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--output", type=Path, default=EVIDENCE_ROOT / "replay.json")
    parser.add_argument("--expect-after", action="store_true",
                        help="Exit unsuccessfully unless both loops would adopt the repaired candidate.")
    args = parser.parse_args()
    root = args.job_dir.resolve()
    output = args.output.resolve()
    if not root.is_relative_to(WORKSPACE) or args.shot < 1 or args.round < 1:
        parser.error("job directory must be in this workspace; shot and round must be positive")
    if not output.is_relative_to(EVIDENCE_ROOT.resolve()) or output.is_relative_to(root):
        parser.error("output must be outside the job and inside reports/assets/P10-05")

    from storycg import cycle, runtime_gate

    sources = {}
    prefix = f"repair_{args.shot}_{args.round}"
    before_file = f"cycle_artifacts/initial_{args.shot}.json"
    after_file = f"cycle_artifacts/{prefix}.json"
    before_neighbors_file = f"cycle_artifacts/{prefix}_before_neighbors.json"
    after_neighbors_file = f"cycle_artifacts/{prefix}_neighbors.json"
    before_audit = read_json(root / before_file, sources)
    after_audit = read_json(root / after_file, sources)
    before_neighbors = read_json(root / before_neighbors_file, sources)
    after_neighbors = read_json(root / after_neighbors_file, sources)
    saved_cycle = read_json(root / "cycle.json", sources)
    state = read_json(root / "state.json", sources)
    history_matches = [
        row for row in saved_cycle["shots"][str(args.shot)]["history"]
        if row["round"] == args.round
    ]
    if len(history_matches) != 1:
        raise ValueError("round does not map to one persisted repair history record")
    history = history_matches[0]
    before_mapping = image_mapping(root, before_audit, state, sources)
    after_mapping = image_mapping(root, after_audit, state, sources)
    if history["source_attempt"] != before_mapping["attempt_number"]:
        raise ValueError("initial audit is not this repair round's incumbent; use its actual saved source")
    if history["output"]["sha256"] != after_mapping["recorded_sha256"]:
        raise ValueError("repair history output does not match the candidate audit")

    results = {
        module.__name__: replay_module(
            module, before_audit, after_audit, before_neighbors, after_neighbors,
            before_mapping, after_mapping,
        )
        for module in (cycle, runtime_gate)
    }
    raw_findings = {
        "before": findings(before_audit, before_neighbors, before_file, before_neighbors_file),
        "after": findings(after_audit, after_neighbors, after_file, after_neighbors_file),
    }
    evidence = {
        "task": "P10-05",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline replay of saved audit records",
        "usage": {"generated_images": 0, "visual_calls": 0, "detector_calls": 0, "network_calls": 0},
        "job_dir": str(root), "shot": args.shot, "round": args.round,
        "severity_order": list(cycle.REPAIR_FAILURE_TIER_NAMES),
        "images": {"before": before_mapping, "after": after_mapping},
        "saved_history_result": {
            key: history["result"].get(key) for key in
            ("reliable_fails_before", "reliable_fails_after", "improved", "improvement_basis", "conclusion")
        },
        "saved_selected_attempt": saved_cycle["shots"][str(args.shot)]["best_attempt"],
        "saved_count_recheck": history.get("count_recheck"),
        "replay": results,
        "reliable_fail_items": {
            side: [row for row in rows if row["item"].get("reliability") == "reliable"
                   and row["item"].get("status") == "fail"]
            for side, rows in raw_findings.items()
        },
        "reliable_not_visible_items": {
            side: [row for row in rows if row["item"].get("reliability") == "reliable"
                   and row["item"].get("status") == "not_visible"]
            for side, rows in raw_findings.items()
        },
        "saved_findings": raw_findings,
        "input_sha256": sources,
        "input_files_unchanged": all(sha256(Path(path)) == digest for path, digest in sources.items()),
        "review_statuses_unchanged": True,
    }
    if not evidence["input_files_unchanged"]:
        raise ValueError("a replay input changed during the run")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output), "replay": results,
        "saved_history_result": evidence["saved_history_result"],
        "input_files_unchanged": evidence["input_files_unchanged"],
    }, ensure_ascii=False, indent=2))
    return int(args.expect_after and not all(row["candidate_improved"] for row in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
