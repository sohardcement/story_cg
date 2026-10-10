"""CLI for one-shot generation, status, and restart-safe recovery."""

import argparse
import json
import sys
from pathlib import Path

from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.identity import resolve_identity
from storycg.pipeline import new_job, plan_job, resolve_job, run_story_job, resolve_and_validate_job
from storycg.runner import resume_job, run_gen_one
from storycg.state import load_state, save_json_atomic
from storycg import review
from storycg.controls import apply_options


def _summary(state: dict) -> dict:
    return {
        "job_id": state["job_id"],
        **({"status":state["status"],"tier_evidence":state.get("tier_evidence",{}),"needs_user":state.get("needs_user",False),"reason":state.get("reason"),"question":state.get("question")} if state.get("status") else {}),
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
    argv = list(argv) if argv is not None else sys.argv[1:]
    if argv and argv[0] == 'detect':
        from storycg.detect import cli
        return cli(argv[1:])
    # Preserve P6's positional make alias for pre-facade jobs.
    if (argv and argv[0] == "make" and len(argv) > 1 and not argv[1].startswith("-")
            and (Path(argv[1]) / "job.json").exists()
            and not (Path(argv[1]) / "agent.json").exists()
            and "--character" not in argv and "--story" not in argv):
        argv[0] = "run"
    if argv and (argv[0] in ("make", "approve", "deliver", "_worker", "compile", "beats-guide") or
                 (argv[0] in ("status", "resume") and len(argv) > 1 and
                  (Path(argv[1]) / "agent.json").exists())):
        from storycg.agent import main as agent_main
        return agent_main(argv)
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
    audit = sub.add_parser("review", help="review existing images without generation")
    audit.add_argument("job_dir", type=Path)
    audit.add_argument("--config", default="config/default.json", type=Path)
    new = sub.add_parser("new", help="create a story job")
    new.add_argument("job_dir", type=Path)
    new.add_argument("--character", required=True)
    new.add_argument("--story", required=True)
    new.add_argument("--shots", type=int, default=4)
    resolve = sub.add_parser("resolve", help="resolve a character using Danbooru")
    resolve.add_argument("job_dir", type=Path)
    resolve.add_argument("--character")
    resolve.add_argument("--work", help="work name or copyright tag for disambiguation")
    resolve.add_argument("--tag", help="explicit canonical character tag")
    plan = sub.add_parser("plan", help="create a schema-validated storyboard")
    plan.add_argument("job_dir", type=Path)
    plan.add_argument("--config", default="config/default.json", type=Path)
    run = sub.add_parser("run", help="resolve, plan, and generate a story job")
    run.add_argument("job_dir", type=Path)
    run.add_argument("--character")
    run.add_argument("--story")
    run.add_argument("--shots", type=int, default=4)
    run.add_argument("--config", default="config/default.json", type=Path)
    new.add_argument('--config', default='config/default.json', type=Path)
    resume.add_argument('--config', default='config/default.json', type=Path)
    for command in (new, run, resume):
        command.add_argument('--identity-control', choices=['none', 'ipadapter'])
        command.add_argument('--seed', type=int)
    args = parser.parse_args(argv)
    options = {key: value for key, value in {
        'identity_control': getattr(args, 'identity_control', None),
        'generation_seed': getattr(args, 'seed', None)}.items() if value is not None}


    if args.command == "review":
        config = (load_state(args.job_dir)["config"]
                  if (args.job_dir / "state.json").exists() else load_config(args.config))
        result = review.review_job(args.job_dir, config)
        print(json.dumps({"conclusion": result["conclusion"],
                          "usage": result.get("usage", {})}, ensure_ascii=False, indent=2))
        return 2 if result["conclusion"] == "未通过" else 0

    if args.command == "new":
        config=apply_options(load_config(args.config), options)
        job=new_job(args.job_dir, args.character, args.story, args.shots,
                    generation_options=options,generation_config=config)
        identity=resolve_and_validate_job(args.job_dir,config)
        print(json.dumps({**job,'identity':identity},ensure_ascii=False,indent=2))
        return 0 if identity.get('identity_tier') in ('L1','L2','L3') else 2
    if args.command == "resolve":
        if (args.job_dir / "job.json").exists():
            result = resolve_job(args.job_dir, work=args.work, character_tag=args.tag)
        elif args.character or args.tag:
            result = resolve_identity(args.character or args.tag, args.job_dir / "cache" / "danbooru",
                                      work=args.work, character_tag=args.tag)
            save_json_atomic(args.job_dir / "identity.json", result)
        else:
            raise ValueError("resolve needs an existing job or --character")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "locked" else 2
    if args.command == "plan":
        result = plan_job(args.job_dir, load_config(args.config))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in ("run", "make") and not (args.job_dir / "job.json").exists():
        if not args.character or not args.story:
            raise ValueError("new run requires --character and --story")
        new_job(args.job_dir, args.character, args.story, args.shots, generation_options=options)
    if args.command == "status":
        state = load_state(args.job_dir)
    else:
        if (args.job_dir / "state.json").exists():
            config = load_state(args.job_dir)["config"]
        else:
            config = load_config(args.config)
        if args.command in ('run', 'make', 'resume'):
            job = json.loads((args.job_dir/'job.json').read_text()) if (args.job_dir/'job.json').exists() else {}
            if not (args.job_dir/'state.json').exists() and job.get('generation_config'):
                config = job['generation_config']
            saved_options = job.get('generation_options', {})
            if (args.job_dir/'state.json').exists():
                frozen = {'identity_control': config.get('identity_control', {}).get('mechanism'),
                          'generation_seed': config.get('generation_seed')}
                if any(frozen.get(k) != v for k,v in options.items()):
                    raise ValueError('existing state has different generation conditions; create a new job')
            elif options:
                saved_options = {**saved_options, **options}
                job['generation_options'] = saved_options
                save_json_atomic(args.job_dir/'job.json', job)
            config = apply_options(config, saved_options)
        if args.command in ("run", "make", "resume"):
            config["repair_cycle_enabled"] = True
        client = ComfyClient(config["comfyui_url"], config["http_timeout_seconds"])
        if args.command in ("run", "make", "resume") and (args.job_dir/"job.json").exists():
            from storycg.budget import Budget, GuardedComfy
            from storycg.cycle import settings
            client = GuardedComfy(client,Budget(args.job_dir,settings()))

        try:
            if args.command == "gen-one":
                state = run_gen_one(args.job_dir, args.shot, args.tags,
                                    args.framing, config, client)
            elif args.command in ("run", "make") or (args.command == "resume" and
                                            (args.job_dir / "job.json").exists()):
                state = run_story_job(args.job_dir, config, client)
            else:
                state = resume_job(args.job_dir, client)
        finally:
            client.close()
    print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
    if state.get("status")=="needs_user" or state.get("status", "").startswith("tier_pending"):
        return 2
    return 0 if all(a["status"] == "completed" for a in state["attempts"]) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"storycg: {exc}", file=sys.stderr)
        sys.exit(1)
