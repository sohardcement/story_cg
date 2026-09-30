"""CLI for one-shot generation, status, and restart-safe recovery."""

import argparse
import json
import sys
from pathlib import Path

from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.identity import resolve_identity
from storycg.pipeline import new_job, plan_job, resolve_job, run_story_job
from storycg.runner import resume_job, run_gen_one
from storycg.state import load_state, save_json_atomic


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
    new = sub.add_parser("new", help="create a story job")
    new.add_argument("job_dir", type=Path)
    new.add_argument("--character", required=True)
    new.add_argument("--story", required=True)
    new.add_argument("--shots", type=int, default=4)
    resolve = sub.add_parser("resolve", help="resolve a character using Danbooru")
    resolve.add_argument("job_dir", type=Path)
    resolve.add_argument("--character")
    plan = sub.add_parser("plan", help="create a schema-validated storyboard")
    plan.add_argument("job_dir", type=Path)
    plan.add_argument("--config", default="config/default.json", type=Path)
    run = sub.add_parser("run", help="resolve, plan, and generate a story job")
    run.add_argument("job_dir", type=Path)
    run.add_argument("--character")
    run.add_argument("--story")
    run.add_argument("--shots", type=int, default=4)
    run.add_argument("--config", default="config/default.json", type=Path)
    args = parser.parse_args(argv)

    if args.command == "new":
        print(json.dumps(new_job(args.job_dir, args.character, args.story, args.shots),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "resolve":
        if (args.job_dir / "job.json").exists():
            result = resolve_job(args.job_dir)
        elif args.character:
            result = resolve_identity(args.character, args.job_dir / "cache" / "danbooru")
            save_json_atomic(args.job_dir / "identity.json", result)
        else:
            raise ValueError("resolve needs an existing job or --character")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "locked" else 2
    if args.command == "plan":
        result = plan_job(args.job_dir, load_config(args.config))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run" and not (args.job_dir / "job.json").exists():
        if not args.character or not args.story:
            raise ValueError("new run requires --character and --story")
        new_job(args.job_dir, args.character, args.story, args.shots)
    if args.command == "status":
        state = load_state(args.job_dir)
    else:
        if (args.job_dir / "state.json").exists():
            config = load_state(args.job_dir)["config"]
        else:
            config = load_config(args.config)
        client = ComfyClient(config["comfyui_url"], config["http_timeout_seconds"])
        try:
            if args.command == "gen-one":
                state = run_gen_one(args.job_dir, args.shot, args.tags,
                                    args.framing, config, client)
            elif args.command == "run" or (args.command == "resume" and
                                            (args.job_dir / "job.json").exists()):
                state = run_story_job(args.job_dir, config, client)
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
