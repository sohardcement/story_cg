import json
from pathlib import Path
from storycg.comfy import ComfyClient
from storycg.config import load_config
from storycg.state import save_json_atomic
c=load_config('config/default.json'); client=ComfyClient(c['comfyui_url'],180)
info=client.http.get('/object_info').json()
assert c['checkpoint'] in info['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0]
for l in c['style_loras']:
    assert l['name'] in info['LoraLoader']['input']['required']['lora_name'][0]
assert 'euler_ancestral' in info['KSampler']['input']['required']['sampler_name'][0]
assert 'normal' in info['KSampler']['input']['required']['scheduler'][0]
result={'queue':client.get_queue(),'models':{},'sampling_source':'https://huggingface.co/LyliaEngine/waiIllustriousSDXL_v170/raw/main/README.md','source_limitation':'Public model-card copy; original Civitai page inaccessible. Scheduler normal is fixed experiment choice.'}
style=json.loads(Path('config/style.json').read_text())
for folder,names in [('checkpoints',[style['checkpoint']]),('loras',[x['name'] for x in style['style_loras']])]:
    files=client.list_model_files(folder)
    for name in names:
        try: metadata=client.get_model_metadata(folder,name)
        except Exception as exc: metadata={'error':str(exc)}
        result['models'][name]={'file':next((x for x in files if x['name']==name),None),'metadata':metadata}
style['model_hashes']={name:{'sha256':'unavailable','reason':'Remote Windows model files are not locally mounted; ComfyUI metadata API does not provide whole-file SHA256.', 'sshs_model_hash': row['metadata'].get('sshs_model_hash','unavailable'),'sshs_model_hash_reason': 'reported by safetensors metadata' if row['metadata'].get('sshs_model_hash') else 'field absent from remote metadata'} for name,row in result['models'].items()}
save_json_atomic('config/style.json',style)
save_json_atomic('runs/p2_01_style_20260930/environment.json',result)
client.close()
print(json.dumps(result,ensure_ascii=False)[:3500])
