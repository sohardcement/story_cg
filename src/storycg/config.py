"""Read and validate the job's generation configuration."""

import copy
import hashlib
import json
import re
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
    if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_-]*/)*", config.get("comfy_prefix_root", "")):
        raise ValueError("comfy_prefix_root must be empty or slash-terminated path segments")
    for framing in ("wide", "full", "medium", "closeup"):
        width, height = config["resolutions"][framing]
        if width <= 0 or height <= 0 or width % 64 or height % 64:
            raise ValueError(f"invalid SDXL resolution for {framing}: {width}x{height}")
    if not isinstance(config["style_loras"], list):
        raise ValueError("style_loras must be a list; an empty list means no style LoRA (V2-20)")
    if config["budget"]["max_images_per_job"] < 1:
        raise ValueError("max_images_per_job must be positive")
    return config


def character_lora_matching_enabled(config: dict | None) -> bool:
    """Whether dedicated character LoRAs are matched and may yield L1 (V2-20, TASK-v2 §10.4).

    Off by default: no character LoRA is matched, L1 is never produced and the L1 vocabulary
    column is not read. Turning it back on is a config change only.
    """
    return bool((config or {}).get("character_lora_matching", False))


DELIVERY_MODES = ("candidates", "repair")


def delivery_mode(config: dict | None) -> str:
    """V2-21 (TASK-v2 §10.3): "candidates" (default: 3 candidates per shot, screen, pick one) or
    "repair" (the legacy repair cycle, kept behind this switch)."""
    value = (config or {}).get("delivery_mode", "candidates")
    if value not in DELIVERY_MODES:
        raise ValueError(f"delivery_mode must be one of {DELIVERY_MODES}")
    return value


def portrait_mode(config: dict | None) -> str:
    """"single": the portrait is used once, its audit is only recorded (default in candidate mode);
    "gated": the legacy D7 gate with Qwen repairs. Defaults follow delivery_mode."""
    default = "single" if delivery_mode(config) == "candidates" else "gated"
    value = (config or {}).get("portrait_mode", default)
    if value not in ("single", "gated"):
        raise ValueError("portrait_mode must be 'single' or 'gated'")
    return value


def candidates_per_shot(config: dict | None) -> int:
    value = (config or {}).get("candidates_per_shot", 3)
    if not isinstance(value, int) or value < 1:
        raise ValueError("candidates_per_shot must be a positive integer")
    return value

