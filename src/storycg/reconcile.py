"""Pure interpretation of a ComfyUI queue/history snapshot."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    prompt_id: str | None = None
    history: dict | None = None


def _prefix(workflow: dict) -> str | None:
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            return node.get("inputs", {}).get("filename_prefix")
    return None


def _queue_entries(queue: dict):
    for location in ("queue_running", "queue_pending"):
        for item in queue.get(location, []):
            yield item[1], item[2]


def _history_entries(history: dict):
    for prompt_id, record in history.items():
        prompt = record.get("prompt") or []
        workflow = prompt[2] if len(prompt) > 2 else {}
        yield prompt_id, workflow, record


def _history_finding(prompt_id: str, record: dict) -> Finding:
    status = record.get("status") or {}
    if status.get("status_str") == "error" or status.get("completed") is False:
        return Finding("failed", prompt_id, record)
    if status.get("status_str") == "success" and status.get("completed") is True:
        return Finding("complete", prompt_id, record)
    return Finding("uncertain", prompt_id, record)


def reconcile_attempt(attempt: dict, queue: dict, history: dict) -> Finding:
    """Interpret one snapshot; the runner rechecks an absent submitting attempt."""
    known_id = attempt.get("prompt_id")
    if known_id and known_id in history:
        return _history_finding(known_id, history[known_id])
    if known_id and any(prompt_id == known_id for prompt_id, _ in _queue_entries(queue)):
        return Finding("queued", known_id)

    prefix = attempt["filename_prefix"]
    matches = [("history", pid, record) for pid, workflow, record in _history_entries(history)
               if _prefix(workflow) == prefix]
    matches += [("queue", pid, None) for pid, workflow in _queue_entries(queue)
                if _prefix(workflow) == prefix]
    distinct_ids = {match[1] for match in matches}
    if len(distinct_ids) > 1:
        return Finding("conflict")
    for source, prompt_id, record in matches:
        if source == "history":
            return _history_finding(prompt_id, record)
    if matches:
        return Finding("queued", matches[0][1])
    if attempt["status"] == "ready":
        return Finding("submit")
    return Finding("uncertain", known_id)
