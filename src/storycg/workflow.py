"""Build a plain ComfyUI API workflow from a config snapshot."""


def build_workflow(config: dict, positive_tags: str, framing: str, seed: int, filename_prefix: str) -> dict:
    if framing not in config["resolutions"]:
        raise ValueError(f"unknown framing: {framing}")
    if not positive_tags.strip():
        raise ValueError("positive tags must not be empty")
    width, height = config["resolutions"][framing]
    first, second = config["style_loras"]
    sampler = config["sampler"]
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": config["checkpoint"]}},
        "2": {"class_type": "LoraLoader", "inputs": {
            "model": ["1", 0], "clip": ["1", 1], "lora_name": first["name"],
            "strength_model": first["strength_model"], "strength_clip": first["strength_clip"],
        }},
        "3": {"class_type": "LoraLoader", "inputs": {
            "model": ["2", 0], "clip": ["2", 1], "lora_name": second["name"],
            "strength_model": second["strength_model"], "strength_clip": second["strength_clip"],
        }},
        "4": {"class_type": "CLIPTextEncode", "inputs": {
            "text": f'{config["positive_prefix"]}, {positive_tags}', "clip": ["3", 1],
        }},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "text": config["negative_prompt"], "clip": ["3", 1],
        }},
        "6": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1,
        }},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["3", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0], "seed": seed, "steps": sampler["steps"],
            "cfg": sampler["cfg"], "sampler_name": sampler["sampler_name"],
            "scheduler": sampler["scheduler"], "denoise": sampler["denoise"],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "images": ["8", 0], "filename_prefix": filename_prefix,
        }},
    }
