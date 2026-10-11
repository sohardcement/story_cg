"""V2-43 verification 2: run `storycg compile` on every beats file under the main workspace jobs/ and this worktree's jobs/.

Usage (from the v2 worktree root):
    PYTHONPATH=$PWD/src .venv/bin/python tools/v2_43_compile_check.py OUT_DIR

Writes OUT_DIR/<label>.json with the exit code and the parsed stdout of each compile. Compile makes no ComfyUI
or vision call; its tag checks read Danbooru, as in production. Scratch job directories are created under
this worktree's compile/ and are not deleted here.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

MAIN = Path('/work/workspace/manga_skill')
WORKTREE = Path(__file__).resolve().parents[1]


def sources():
    found = []
    for label, base in (('main', MAIN / 'jobs'), ('wt', WORKTREE / 'jobs')):
        for beats in sorted(base.glob('*/beats.json')):
            found.append((label, beats))
    return found


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PYTHONPATH': str(WORKTREE / 'src')}
    for label, beats in sources():
        job = json.loads((beats.parent / 'job.json').read_text(encoding='utf-8'))
        command = [str(WORKTREE / '.venv/bin/python'), '-m', 'storycg', 'compile',
                   '--character', job['character'], '--beats', str(beats), '--shots', str(job['shot_count'])]
        proc = subprocess.run(command, cwd=str(WORKTREE), env=env, capture_output=True, text=True, timeout=900)
        try:
            parsed = json.loads(proc.stdout)
        except ValueError:
            parsed = {'unparsed_stdout': proc.stdout[-4000:], 'stderr_tail': proc.stderr[-4000:]}
        name = f'{label}-{beats.parent.name}.json'
        record = {'source': str(beats), 'character': job['character'], 'shots': job['shot_count'],
                  'exit': proc.returncode, 'output': parsed}
        (out / name).write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        print(name, proc.returncode, parsed.get('status') if isinstance(parsed, dict) else None, flush=True)


if __name__ == '__main__':
    main(sys.argv[1])
