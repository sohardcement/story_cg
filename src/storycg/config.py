"""Read and validate the job's generation configuration."""

import copy
import hashlib
import json
from pathlib import Path


def load_config(path: str | Path) -> dict:
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("style_file"):
        style_path = path.parent / config["style_file"]
        raw = style_path.read_bytes()
        style = json.loads(raw)
        for key in ("checkpoint", "style_loras", "sampler", "resolutions", "positive_prefix", "negative_prompt"):
            config[key] = copy.deepcopy(style[key])
        config["style_snapshot"] = {"path": str(style_path),
                                    "sha256": hashlib.sha256(raw).hexdigest(), "content": style}
    if config["http_timeout_seconds"] < 180:
        raise ValueError("http_timeout_seconds must be at least 180")
    for framing in ("wide", "full", "medium", "closeup"):
        width, height = config["resolutions"][framing]
        if width <= 0 or height <= 0 or width % 64 or height % 64:
            raise ValueError(f"invalid SDXL resolution for {framing}: {width}x{height}")
    if len(config["style_loras"]) != 2:
        raise ValueError("exactly two style LoRAs are required")
    if config["budget"]["max_images_per_job"] < 1:
        raise ValueError("max_images_per_job must be positive")
    return config
