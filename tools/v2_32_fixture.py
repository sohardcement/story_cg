"""V2-32: build the test fixture and the offline copies from real task records (read-only on the source).

fixture SRC DST   copy the JSON records the display tests need, shrink the candidate and portrait PNGs
                  (long side <= 256) into DST (tests/fixtures/v2_32).
offline SRC DST   copy a whole job into DST (reports/assets/V2-32/...) and shrink every PNG/JPG inside
                  the copy (long side <= 256). Used to re-run `deliver` offline; pixels are not read by
                  result.md, only the sheets are rebuilt from them.
Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_32_fixture.py fixture SRC DST
"""
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

FIXTURE_JSON = ['candidates.json', 'storyboard.json', 'portrait.json', 'portrait_attempts.json', 'manifest.json',
                'cycle.json', 'agent.json', 'job.json', 'identity.json', 'state.json', 'beats.json',
                'plan_log.json', 'review_gate.json']


def shrink(path, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        image = image.convert('RGB')
        scale = min(1.0, 256 / max(image.size))
        small = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        if target.suffix.lower() == '.png':
            small.save(target, 'PNG')
        else:
            small.save(target, 'JPEG', quality=90)


def fixture(src_arg, dst_arg):
    src, dst = Path(src_arg).resolve(), Path(dst_arg).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for name in FIXTURE_JSON:
        if (src / name).exists():
            shutil.copyfile(src / name, dst / name)
    (dst / 'cycle_artifacts').mkdir(exist_ok=True)
    for path in sorted((src / 'cycle_artifacts').glob('cand_*.json')):
        if re.fullmatch(r'cand_\d{2}_\d{2}\.json', path.name):
            shutil.copyfile(path, dst / 'cycle_artifacts' / path.name)
    images = sorted(src.glob('storycg_*_a00*.png')) + sorted((src / 'portrait').glob('*.png'))
    for path in images:
        shrink(path, dst / path.relative_to(src))
    print(f'json {len(FIXTURE_JSON)}, cycle_artifacts audits {len(list((dst / "cycle_artifacts").glob("*.json")))}, images {len(images)}')


def offline(src_arg, dst_arg):
    src, dst = Path(src_arg).resolve(), Path(dst_arg).resolve()
    if dst.exists():
        raise SystemExit(f'destination exists: {dst}')
    shutil.copytree(src, dst)
    count = 0
    for path in sorted(dst.rglob('*')):
        if path.is_file() and path.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            shrink(path, path)
            count += 1
    print(f'offline copy {dst}: shrunk {count} images')


if __name__ == '__main__':
    command, src, dst = sys.argv[1:4]
    {'fixture': fixture, 'offline': offline}[command](src, dst)
