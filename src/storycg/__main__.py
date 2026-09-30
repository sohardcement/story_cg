"""CLI for one-shot generation, status, and restart-safe recovery."""

import argparse
import json
import sys
from pathlib import Path

from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.runner import resume_job, run_gen_one
from storycg.state import load_state


def _summary(state: dict) -> dict:
    return {
        "job_id": state["job_id"],
        "stats": state.get("stats", {}),
        "attempts": [{
            "shot_id": attempt["shot_id"],
            "attempt_number": attempt["attempt_number"],
            "status": attempt["status"],
            "prompt_id": attempt.get("prompt_id"),
            "filename_prefix": attempt.get("filename_prefix"),
            "image": attempt.get("image"),
            "error": attempt.get("error"),
        } for attempt in state["attempts"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m storycg")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("gen-one", help="generate one persisted shot")
    gen.add_argument("job_dir", type=Path)
    gen.add_argument("--shot", required=True)
    gen.add_argument("--tags", required=True)
    gen.add_argument("--framing", required=True)
    gen.add_argument("--config", default="config/default.json", type=Path)
    status = sub.add_parser("status", help="print persisted state")
    status.add_argument("job_dir", type=Path)
    resume = sub.add_parser("resume", help="reconcile queue/history and continue")
    resume.add_argument("job_dir", type=Path)
    args = parser.parse_args(argv)

    if args.command == "status":
        state = load_state(args.job_dir)
    else:
        if args.command == "gen-one" and not (args.job_dir / "state.json").exists():
            config = load_config(args.config)
        else:
            config = load_state(args.job_dir)["config"]
        client = ComfyClient(config["comfyui_url"], config["http_timeout_seconds"])
        try:
            if args.command == "gen-one":
                state = run_gen_one(args.job_dir, args.shot, args.tags,
                                    args.framing, config, client)
            else:
                state = resume_job(args.job_dir, client)
        finally:
            client.close()
    print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
    return 0 if all(a["status"] == "completed" for a in state["attempts"]) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"storycg: {exc}", file=sys.stderr)
        sys.exit(1)
