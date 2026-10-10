"""Build a plain ComfyUI API workflow from a config snapshot."""


def style_lora_chain(config: dict):
    """Style LoRA nodes in config order (ids 2, 3, ...), plus the MODEL and CLIP tails.

    With no style LoRA the chain is empty and the tails are the checkpoint outputs (V2-20).
    Two style LoRAs give the same nodes "2" and "3" as before; a third and later one use 40, 41, ...
    """
    nodes = {}
    model, clip = ["1", 0], ["1", 1]
    for index, lora in enumerate(config["style_loras"]):
        # Ids 2 and 3 are kept for compatibility; later LoRAs use 40, 41, ... because 4-13,
        # 20-23 and 30 are taken by the sampler, text, IPAdapter, identity and character LoRA nodes.
        node_id = str(index + 2) if index < 2 else str(40 + index - 2)
        nodes[node_id] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": lora["name"],
            "strength_model": lora["strength_model"], "strength_clip": lora["strength_clip"],
        }}
        model, clip = [node_id, 0], [node_id, 1]
    return nodes, model, clip


def build_workflow(config: dict, positive_tags: str, framing: str, seed: int, filename_prefix: str) -> dict:
    if framing not in config["resolutions"]:
        raise ValueError(f"unknown framing: {framing}")
    if not positive_tags.strip():
        raise ValueError("positive tags must not be empty")
    width, height = config["resolutions"][framing]
    if config.get("generation_reference", {}).get("mechanism") == "qwen_edit":
        return build_qwen_workflow(config, config["generation_reference"]["uploaded_image"], positive_tags, width, height, seed, filename_prefix)
    lora_nodes, model_ref, clip_ref = style_lora_chain(config)
    sampler = config["sampler"]
    workflow = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": config["checkpoint"]}}}
    workflow.update(lora_nodes)
    workflow.update({
        "4": {"class_type": "CLIPTextEncode", "inputs": {
            "text": f'{config["positive_prefix"]}, {positive_tags}', "clip": clip_ref,
        }},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "text": config["negative_prompt"], "clip": clip_ref,
        }},
        "6": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1,
        }},
        "7": {"class_type": "KSampler", "inputs": {
            "model": model_ref, "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0], "seed": seed, "steps": sampler["steps"],
            "cfg": sampler["cfg"], "sampler_name": sampler["sampler_name"],
            "scheduler": sampler["scheduler"], "denoise": sampler["denoise"],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "images": ["8", 0], "filename_prefix": filename_prefix,
        }},
    })

    reference = config.get("generation_reference")
    if reference:
        workflow["10"] = {"class_type": "LoadImage", "inputs": {"image": reference["uploaded_image"]}}
        if reference["mechanism"] == "ipadapter":
            workflow["11"] = {"class_type": "IPAdapterModelLoader", "inputs": {"ipadapter_file": config["reference_models"]["ipadapter"]}}
            workflow["13"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": config["reference_models"]["clip_vision"]}}
            workflow["12"] = {"class_type": "IPAdapterAdvanced", "inputs": {
                "model": model_ref, "ipadapter": ["11", 0], "image": ["10", 0],
                "clip_vision": ["13", 0], "weight": reference["strength"], "weight_type": "linear",
                "combine_embeds": "concat", "start_at": 0.0, "end_at": 1.0, "embeds_scaling": "V only"}}
            workflow["7"]["inputs"]["model"] = ["12", 0]
        elif reference["mechanism"] == "img2img":
            workflow["11"] = {"class_type": "ImageScale", "inputs": {"image": ["10",0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}}
            workflow["12"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["11",0], "vae": ["1",2]}}
            workflow["7"]["inputs"].update(latent_image=["12",0],denoise=reference["strength"])
        else:
            raise ValueError("unknown reference mechanism")
    identity_reference = (reference or {}).get("identity_reference")
    if identity_reference:
        workflow["20"] = {"class_type": "LoadImage", "inputs": {"image": identity_reference["uploaded_image"]}}
        workflow["21"] = {"class_type": "IPAdapterModelLoader", "inputs": {"ipadapter_file": config["reference_models"]["ipadapter"]}}
        workflow["22"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": config["reference_models"]["clip_vision"]}}
        workflow["23"] = {"class_type": "IPAdapterAdvanced", "inputs": {
            "model": model_ref, "ipadapter": ["21", 0], "image": ["20", 0],
            "clip_vision": ["22", 0], "weight": identity_reference["strength"], "weight_type": "linear",
            "combine_embeds": "concat", "start_at": 0.0, "end_at": 1.0, "embeds_scaling": "V only"}}
        workflow["7"]["inputs"]["model"] = ["23", 0]
    if config.get("character_lora"):
        from storycg.tiers import add_character_lora
        lora = config["character_lora"]
        workflow = add_character_lora(workflow, lora, model_ref, clip_ref)
        triggers = ", ".join(lora.get("trigger_words", []))
        if triggers:
            workflow["4"]["inputs"]["text"] += ", " + triggers
    return workflow


def build_qwen_workflow(config, uploaded_image, prompt, width, height, seed, prefix):
    models=config['reference_models']
    return {
        '1':{'class_type':'UnetLoaderGGUF','inputs':{'unet_name':models['qwen_unet']}},
        '2':{'class_type':'LoraLoaderModelOnly','inputs':{'model':['1',0],'lora_name':models['qwen_lightning'],'strength_model':1.0}},
        '3':{'class_type':'ModelSamplingAuraFlow','inputs':{'model':['2',0],'shift':3.0}},
        '4':{'class_type':'TextEncodeQwenImageEditPlus','inputs':{'clip':['11',0],'vae':['12',0],'image1':['10',0],'prompt':prompt}},
        '5':{'class_type':'TextEncodeQwenImageEditPlus','inputs':{'clip':['11',0],'vae':['12',0],'image1':['10',0],'prompt':''}},
        '6':{'class_type':'EmptySD3LatentImage','inputs':{'width':width,'height':height,'batch_size':1}},
        '7':{'class_type':'KSampler','inputs':{'model':['3',0],'positive':['4',0],'negative':['5',0], 'latent_image':['6',0], 'seed':seed,'steps':4,'cfg':1.0,'sampler_name':'euler','scheduler':'simple','denoise':1.0}},
        '8':{'class_type':'VAEDecode','inputs':{'samples':['7',0],'vae':['12',0]}},
        '9':{'class_type':'SaveImage','inputs':{'images':['8',0],'filename_prefix':prefix}},
        '10':{'class_type':'LoadImage','inputs':{'image':uploaded_image}},
        '11':{'class_type':'CLIPLoader','inputs':{'clip_name':models['qwen_clip'],'type':'qwen_image','device':'default'}},
        '12':{'class_type':'VAELoader','inputs':{'vae_name':models['qwen_vae']}},
    }
