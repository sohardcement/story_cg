"""V2-43b verification 2: compare compile outputs with the V2-43 baseline and classify every difference.

Usage (from the v2 worktree root):
    .venv/bin/python -I tools/v2_43b_compare.py BASE_DIR AFTER_DIR

Each leaf that differs is printed with its path and a class. Classes:
- identity_tier:      the identity_tier field (allowed by TASK-v2 V2-43b: it disappears)
- tier_guide:         tier / tier_known / tier_note / vocab_column in character_guide (removed by V2-43b)
- lora_sentence:      the vocabulary text loses the "Character LoRA is off" sentence and the "层级" note
- tmp_path:           compile scratch directory uuid paths (output/job_dir)
- post_count:         Danbooru live post_count
- unexplained:        anything else (must be 0)
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

LORA = ' Character LoRA is off: the L2 column applies to every tier.'
TIER_GUIDE = {'tier', 'tier_known', 'tier_note', 'vocab_column'}


def classify(path, base, after):
    leaf = path.rsplit('/', 1)[-1]
    if leaf == 'identity_tier':
        return 'identity_tier'
    if '/character_guide/' in path and leaf in TIER_GUIDE:
        return 'tier_guide'
    if leaf == 'vocab' and isinstance(base, str) and isinstance(after, str) and base.replace(LORA, '') == after:
        return 'lora_sentence'
    if leaf == 'job_dir' or leaf == 'output' or path.startswith('/output/') or 'compile/cmp-' in str(base):
        return 'tmp_path'
    if leaf == 'post_count':
        return 'post_count'
    return 'unexplained'


def walk(base, after, path, out):
    if isinstance(base, dict) and isinstance(after, dict):
        for key in sorted(set(base) | set(after)):
            sub = f'{path}/{key}'
            if key not in after:
                out.append((sub, base[key], '<missing>'))
            elif key not in base:
                out.append((sub, '<missing>', after[key]))
            else:
                walk(base[key], after[key], sub, out)
    elif isinstance(base, list) and isinstance(after, list) and len(base) == len(after):
        for index, (b, a) in enumerate(zip(base, after)):
            walk(b, a, f'{path}[{index}]', out)
    elif base != after:
        out.append((path, base, after))


def main(base_dir, after_dir):
    base_dir, after_dir = Path(base_dir), Path(after_dir)
    counts = Counter()
    lines = []
    for base_file in sorted(base_dir.glob('*.json')):
        after_file = after_dir / base_file.name
        if not after_file.exists():
            lines.append(f'{base_file.name}: missing in after')
            counts['missing'] += 1
            continue
        base = json.loads(base_file.read_text(encoding='utf-8'))
        after = json.loads(after_file.read_text(encoding='utf-8'))
        diffs = []
        walk(base, after, '', diffs)
        for path, b, a in diffs:
            kind = classify(path, b, a)
            counts[kind] += 1
            if kind != 'post_count' and kind != 'tmp_path':
                lines.append(f'{base_file.name} [{kind}] {path}: {str(b)[:90]!r} -> {str(a)[:90]!r}')
        if not diffs:
            counts['identical'] += 1
        status_same = base.get('output', {}).get('status') == after.get('output', {}).get('status')
        exit_same = base.get('exit') == after.get('exit')
        if not (status_same and exit_same):
            lines.append(f'{base_file.name} [status/exit] base={base.get("exit")} after={after.get("exit")}')
            counts['status_or_exit'] += 1
    print('\n'.join(lines))
    print('SUMMARY', dict(counts))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
