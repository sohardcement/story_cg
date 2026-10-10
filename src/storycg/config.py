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
    check_removed_switches(config)
    return config


def character_lora_matching_enabled(config: dict | None) -> bool:
    """Whether dedicated character LoRAs are matched and may yield L1 (V2-20, TASK-v2 §10.4).

    Off by default: no character LoRA is matched, L1 is never produced and the L1 vocabulary
    column is not read. Turning it back on is a config change only.
    """
    return bool((config or {}).get("character_lora_matching", False))


class RemovedMode(ValueError):
    """A job snapshot or config still asks for a delivery path that V2-28 removed."""


# V2-28: the repair delivery, the gated portrait and the Qwen layout were removed. Only the value on the
# left is still accepted (old snapshots carry it); any other value is refused with the message.
REMOVED_SWITCHES = {
    "delivery_mode": ("candidates", "修图方式已删除（V2-28）：配置或任务快照里是 delivery_mode={value!r}，"
                      "现在只有候选交付（每镜 3 张候选、筛选、自动挑一张）；请新建任务。"),
    "portrait_mode": ("single", "定妆图门禁已删除（V2-28）：配置或任务快照里是 portrait_mode={value!r}，"
                      "现在定妆图只出 1 张、审核只记录；请新建任务。"),
    "layout_mode": ("none", "Qwen 布局已删除（V2-28）：配置或任务快照里是 layout_mode={value!r}，"
                    "现在不再使用 Qwen；请新建任务。"),
}


def check_removed_switches(config: dict | None) -> None:
    """Raise RemovedMode when a config or job snapshot asks for a removed delivery path."""
    for key, (kept, message) in REMOVED_SWITCHES.items():
        value = (config or {}).get(key, kept)
        if value != kept:
            raise RemovedMode(message.format(value=value))


def candidates_per_shot(config: dict | None) -> int:
    value = (config or {}).get("candidates_per_shot", 3)
    if not isinstance(value, int) or value < 1:
        raise ValueError("candidates_per_shot must be a positive integer")
    return value


def pairwise_enabled(config: dict | None) -> bool:
    """V2-23 (D65): side-by-side pick between tied top candidates. Off gives the V2-21 behaviour."""
    return bool((config or {}).get("pairwise_pick", True))

