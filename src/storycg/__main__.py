"""Command entry point: every story command goes through storycg.agent (V2-29 kept one command set)."""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    from storycg.agent import main as agent_main
    return agent_main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"storycg: {exc}", file=sys.stderr)
        sys.exit(1)
