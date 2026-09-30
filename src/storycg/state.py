"""Durable per-job state and safe names for ComfyUI output prefixes."""

import json
import os
import re
import tempfile
from pathlib import Path


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def make_prefix(job_id: str, shot_id: str, attempt_number: int) -> str:
    if not IDENTIFIER.fullmatch(job_id) or not IDENTIFIER.fullmatch(shot_id):
        raise ValueError("job and shot IDs must use letters, digits, '-' or '_'")
    if attempt_number < 1:
        raise ValueError("attempt number must be positive")
    return f"storycg_{job_id}_{shot_id}_a{attempt_number:03d}"


def save_json_atomic(path: str | Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def load_state(job_dir: str | Path) -> dict:
    return json.loads((Path(job_dir) / "state.json").read_text(encoding="utf-8"))


def save_state(job_dir: str | Path, state: dict) -> None:
    save_json_atomic(Path(job_dir) / "state.json", state)
