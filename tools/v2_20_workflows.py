"""V2-20 regression helper: dump workflows built from a fixed config.

Run with PYTHONPATH pointing at the storycg source tree under test. The config is the
repository default with the two style LoRAs written explicitly, so the same input works
before and after the V2-20 change. Output order is preserved (no sort_keys) on purpose.
"""
import json
import sys
from pathlib import Path

from storycg.config import load_config
from storycg.workflow import build_workflow

TWO_STYLE = [
    {"name": "Iwao178.safetensors", "strength_model": 0.4, "strength_clip": 0.4},
    {"name": "akaimelon.safetensors", "strength_model": 0.3, "strength_clip": 0.3},
]
CHARACTER = {"name": "character_test.safetensors", "strength_model": 0.8, "strength_clip": 0.8,
             "trigger_words": ["testtrigger"]}
TAGS = "solo, standing, looking at viewer, simple background"


def main(config_path, out_path, style):
    base = load_config(config_path)
    base["style_loras"] = [] if style == "none" else [dict(x) for x in TWO_STYLE]
    cases = {}
    for framing in ("wide", "full", "medium", "closeup"):
        cases[f"plain_{framing}"] = build_workflow(base, TAGS, framing, 1001, "v2/test_plain")
    ipa = {"mechanism": "ipadapter", "strength": 0.4, "uploaded_image": "face.png", "path": "face.png", "sha256": "0"}
    cfg = {**base, "generation_reference": ipa}
    cases["ipadapter_medium"] = build_workflow(cfg, TAGS, "medium", 1002, "v2/test_ipa")
    cfg2 = {**base, "generation_reference": {**ipa, "identity_reference": {"uploaded_image": "id.png", "strength": 0.4}}}
    cases["identity_medium"] = build_workflow(cfg2, TAGS, "medium", 1003, "v2/test_id")
    cfg3 = {**base, "generation_reference": {"mechanism": "img2img", "strength": 0.5, "uploaded_image": "src.png"}}
    cases["img2img_full"] = build_workflow(cfg3, TAGS, "full", 1004, "v2/test_img")
    cases["character_lora_plain"] = build_workflow({**base, "character_lora": dict(CHARACTER)}, TAGS, "medium", 1005, "v2/test_char")
    cases["character_lora_identity"] = build_workflow({**cfg2, "character_lora": dict(CHARACTER)}, TAGS, "medium", 1006, "v2/test_char_id")
    Path(out_path).write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(cases)} workflows to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "two")
