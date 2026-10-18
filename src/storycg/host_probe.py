"""Tell the caller early that the ComfyUI host is slow, instead of the session running out at about 600 s (2026-10-18, after loop B).

Two checks, both only read: ComfyUI's /queue and /history, or the job's own state.json. Thresholds are config `host_probe`; a job whose
config snapshot has no `host_probe` (made before this) is not checked.
- start_warning (make, before the worker starts): /queue does not answer within timeout_seconds; the images other jobs already queued
  would take longer than queue_wait_seconds; or the host's recent images took longer than slow_image_seconds each.
- run_warning (status, while the worker runs): the job's newest image, finished or still waiting, took longer than slow_image_seconds.
Loop B on this host: 8-24 s per image (submission to completion) in 8 normal runs. Attempt 4's slowdown began at its 4th image (78 s,
then up to 428 s) after three normal ones, so the start check alone would not have seen it; run_warning would have, at that image.
"""
from datetime import datetime, timezone
from statistics import median

import httpx

# Per-image seconds assumed for the queue estimate when /history is empty (right after a ComfyUI restart): loop B's normal median.
UNKNOWN_IMAGE_SECONDS = 12
HISTORY_ITEMS = 8


def _execution_seconds(history):
    """Execution seconds (execution_start to execution_success) of each finished /history entry; fully cached runs are skipped."""
    out = []
    for entry in (history or {}).values():
        stamps = {name: (body or {}).get('timestamp') for name, body in ((entry.get('status') or {}).get('messages') or [])
                  if isinstance(body, dict)}
        if stamps.get('execution_start') and stamps.get('execution_success'):
            seconds = (stamps['execution_success'] - stamps['execution_start']) / 1000
            if seconds >= 1:
                out.append(seconds)
    return out


def start_warning(config, our_images, *, transport=None):
    """None, or one line for the caller saying the host looks too slow for this job. Errors other than a timeout return None: an
    unreachable host is reported by the worker's resource check as before."""
    s = config.get('host_probe')
    if not s:
        return None
    try:
        with httpx.Client(base_url=config['comfyui_url'], timeout=s['timeout_seconds'], trust_env=False, transport=transport) as client:
            response = client.get('/queue'); response.raise_for_status()
            queue = response.json()
            response = client.get('/history', params={'max_items': HISTORY_ITEMS}); response.raise_for_status()
            recent = _execution_seconds(response.json())
    except httpx.TimeoutException:
        return f"ComfyUI 主机慢：{s['timeout_seconds']} 秒内没有回应队列查询，预计超时。"
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    per_image = median(recent) if recent else None
    if per_image is not None and per_image > s['slow_image_seconds']:
        return (f"ComfyUI 主机慢：最近 {len(recent)} 张图每张中位 {per_image:.0f} 秒（超过 {s['slow_image_seconds']} 秒算慢）。"
                f"这次至少 {our_images} 张，照这个速度要 {our_images * per_image / 60:.0f} 分钟以上，预计超时。")
    ahead = len(queue.get('queue_running') or []) + len(queue.get('queue_pending') or [])
    wait = ahead * (per_image or UNKNOWN_IMAGE_SECONDS)
    if wait > s['queue_wait_seconds']:
        return (f"ComfyUI 主机忙：队列里已有 {ahead} 个别的任务，估计要先等约 {wait:.0f} 秒（超过 {s['queue_wait_seconds']} 秒算慢），"
                f"预计超时。")
    return None


def run_warning(attempts, config, remaining, now=None):
    """None, or one line: the job's newest image took (or has waited) longer than slow_image_seconds. remaining = images still to make."""
    s = config.get('host_probe')
    started = [(datetime.fromisoformat(a['submission_started_at']), n, a) for n, a in enumerate(attempts) if a.get('submission_started_at')]
    if not s or not started:
        return None
    begin, _, latest = max(started, key=lambda row: row[:2])                      # the same second: the later attempt
    if latest.get('completed_at'):
        seconds, what = (datetime.fromisoformat(latest['completed_at']) - begin).total_seconds(), '最近一张图用了'
    elif latest.get('status') in ('submitted', 'queued', 'waiting'):
        seconds, what = ((now or datetime.now(timezone.utc)) - begin).total_seconds(), '当前这张图已经等了'
    else:
        return None
    if seconds <= s['slow_image_seconds']:
        return None
    rest = f"照这个速度剩下约 {remaining} 张还要约 {remaining * seconds / 60:.0f} 分钟，" if remaining else ''
    return f"ComfyUI 主机慢：{what} {seconds:.0f} 秒（超过 {s['slow_image_seconds']} 秒算慢）。{rest}预计超时。"
