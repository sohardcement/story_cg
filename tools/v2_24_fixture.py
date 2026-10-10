"""V2-24: copy the smoke job's JSON files and shrink its PNGs (long side <= 256) into tests/fixtures/v2_24/.

Read-only on the source job; the destination is written only inside the worktree.
Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_24_fixture.py jobs/cg-8092d80fd20b tests/fixtures/v2_24
"""
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

JSON_FILES = ['candidates.json', 'storyboard.json', 'portrait.json', 'portrait_attempts.json', 'manifest.json',
              'review.json', 'cycle.json', 'agent.json', 'job.json', 'identity.json', 'state.json', 'beats.json']


def main(src_arg, dst_arg):
    src, dst = Path(src_arg).resolve(), Path(dst_arg).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for name in JSON_FILES:
        shutil.copyfile(src / name, dst / name)
    (dst / 'cycle_artifacts').mkdir(exist_ok=True)
    for path in sorted((src / 'cycle_artifacts').glob('cand_*.json')):
        if re.fullmatch(r'cand_\d{2}_\d{2}\.json', path.name):  # per-candidate audits only
            shutil.copyfile(path, dst / 'cycle_artifacts' / path.name)
    images = sorted(src.glob('storycg_*_a00*.png')) + sorted((src / 'portrait').glob('*.png'))
    for path in images:
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image = image.convert('RGB')
            scale = min(1.0, 256 / max(image.size))
            small = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
            small.save(target, 'PNG')
    print(f'json {len(JSON_FILES)}, cycle_artifacts {len(list((dst / "cycle_artifacts").iterdir()))}, images {len(images)}')


if __name__ == '__main__':
    main(*sys.argv[1:3])
