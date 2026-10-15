"""V2-59 (D96): `storycg prune` clears intermediate files that are no longer used once a job is delivered.

Two kinds of item, both only inside the project's compile/ and jobs/ folders:
- compile/cmp-* scratch folders whose newest file is older than N days: the whole folder;
- jobs/cg-* folders whose job has ended (completed, stopped, needs_user or cancelled), whose agent.json is older
  than N days and whose worker is not running: the *.png files of cycle_artifacts/ (that level only) and of
  review_artifacts/ (any depth). Everything else in the job stays.

Symbolic links are never followed and never deleted. Nothing is deleted without --apply.
"""
import argparse
import json
import os
import shutil
import time
from pathlib import Path

from storycg.agent import worker_alive

PROJECT = Path(__file__).resolve().parents[2]
ENDED_PHASES = ('completed', 'stopped', 'needs_user', 'cancelled')
DAY_SECONDS = 86400


def _under(path, base):
    """True when path, with links resolved, lies strictly inside base."""
    resolved, top = path.resolve(), base.resolve()
    return resolved != top and resolved.is_relative_to(top)


def _delete(path, base, skipped, directory):
    """Delete one planned entry. Links and anything resolving outside base are refused; failures are recorded."""
    if path.is_symlink() or not _under(path, base):
        skipped.append({'path': str(path), 'reason': 'outside the project folder or a symbolic link'})
        return False
    try:
        if directory:
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        skipped.append({'path': str(path), 'reason': f'delete failed: {exc}'})
        return False
    return True


def _raise(exc):
    raise exc


def _scan(folder):
    """Plain files under folder as (path, stat), and the symbolic links met there. Links are not followed.
    An unreadable part raises OSError, so its folder is kept rather than judged by the readable part."""
    files, links = [], []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=_raise):
        here = Path(dirpath)
        for name in dirnames + filenames:
            path = here / name
            if path.is_symlink():
                links.append(path)
            elif name in filenames:
                files.append((path, path.lstat()))
    return files, links


def _compile_plan(root, cutoff, skipped):
    """(folder, bytes) for each compile/cmp-* scratch folder whose newest file is older than the cutoff."""
    base = root / 'compile'
    plan = []
    if base.is_symlink():
        skipped.append({'path': str(base), 'reason': 'symbolic link'})
        return plan
    if not base.is_dir():
        return plan
    for folder in sorted(base.iterdir()):
        if not folder.name.startswith('cmp-'):
            continue
        if folder.is_symlink() or not folder.is_dir():
            reason = 'symbolic link' if folder.is_symlink() else 'not a directory'
            skipped.append({'path': str(folder), 'reason': reason})
            continue
        try:
            files, links = _scan(folder)
            newest = max((st.st_mtime for _, st in files), default=folder.lstat().st_mtime)
        except OSError as exc:
            skipped.append({'path': str(folder), 'reason': f'unreadable: {exc}'})
            continue
        if links:
            # Deleting the folder would delete the link too, so the whole folder stays.
            skipped.append({'path': str(folder), 'reason': f'contains a symbolic link ({links[0].relative_to(folder)})'})
            continue
        if newest < cutoff:
            plan.append((folder, sum(st.st_size for _, st in files)))
    return plan


def _record(job):
    """agent.json of a job as a dict, or None when it is missing, a link, unreadable or not an object."""
    path = job / 'agent.json'
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _collect_pngs(folder, recursive, pngs, links):
    with os.scandir(folder) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                if recursive:
                    _collect_pngs(path, True, pngs, links)
            elif entry.name.endswith('.png') and entry.is_file(follow_symlinks=False):
                pngs.append((path, entry.stat(follow_symlinks=False).st_size))


def _artifact_pngs(job):
    """PNG files prune may delete in one job (with sizes), and the symbolic links met in its artifact folders."""
    pngs, links = [], []
    for name, recursive in (('cycle_artifacts', False), ('review_artifacts', True)):
        folder = job / name
        if folder.is_symlink():
            links.append(folder)
        elif folder.is_dir():
            _collect_pngs(folder, recursive, pngs, links)
    return pngs, links


def _job_plan(root, cutoff, skipped):
    """(job, [(png, bytes)]) for each ended, old job with PNG files to remove."""
    base = root / 'jobs'
    plan = []
    if not base.is_dir():
        return plan
    for job in sorted(base.iterdir()):
        if not job.name.startswith('cg-'):
            continue
        if job.is_symlink() or not job.is_dir():
            reason = 'symbolic link' if job.is_symlink() else 'not a directory'
            skipped.append({'path': str(job), 'reason': reason})
            continue
        record = _record(job)
        if record is None:
            skipped.append({'path': str(job), 'reason': 'agent.json missing, unreadable or a symbolic link'})
            continue
        phase = record.get('phase')
        if phase not in ENDED_PHASES:
            skipped.append({'path': str(job), 'reason': f'phase {phase!r} is not finished'})
            continue
        if (job / 'agent.json').stat().st_mtime >= cutoff:
            continue
        if worker_alive(job):
            skipped.append({'path': str(job), 'reason': 'worker is running'})
            continue
        try:
            pngs, links = _artifact_pngs(job)
        except OSError as exc:
            skipped.append({'path': str(job), 'reason': f'unreadable: {exc}'})
            continue
        skipped.extend({'path': str(link), 'reason': 'symbolic link'} for link in links)
        if pngs:
            plan.append((job, pngs))
    return plan


def prune(root, days=7, apply=False):
    """Plan, and with apply perform, the cleanup under root. Returns the report prune_cli prints."""
    cutoff = time.time() - days * DAY_SECONDS
    skipped = []
    compile_dirs, job_files, total = [], {}, 0
    for folder, nbytes in _compile_plan(root, cutoff, skipped):
        if apply and not _delete(folder, root / 'compile', skipped, directory=True):
            continue
        compile_dirs.append({'path': str(folder), 'bytes': nbytes})
        total += nbytes
    for job, pngs in _job_plan(root, cutoff, skipped):
        files = freed = 0
        for png, nbytes in pngs:
            if apply and not _delete(png, root / 'jobs', skipped, directory=False):
                continue
            files += 1
            freed += nbytes
        if files:
            job_files[str(job)] = {'files': files, 'bytes': freed}
            total += freed
    return {'applied': apply, 'days': days, 'root': str(root), 'compile_dirs': compile_dirs,
            'job_files': job_files, 'skipped': skipped, 'total_bytes': total}


def prune_cli(argv):
    parser = argparse.ArgumentParser(prog='storycg prune',
                                     description="Remove finished jobs' intermediate PNG files and old compile scratch "
                                                 'folders. Nothing is deleted without --apply.')
    parser.add_argument('--days', type=int, default=7, help='only items untouched for N days (default 7)')
    parser.add_argument('--apply', action='store_true', help='delete; without it only the JSON report is printed')
    parser.add_argument('--root', type=Path, help='project root to inspect (default: this checkout)')
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error('--days must be at least 1')
    report = prune((args.root or PROJECT).resolve(), args.days, args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
