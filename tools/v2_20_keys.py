"""V2-20: sample cache keys. Run with PYTHONPATH at the tree under test; prints JSON."""
import copy
import json
import sys
from storycg.config import load_config
from storycg import reference
from storycg.tier_cache import model_fingerprint, cache_key

FILES = {'checkpoints': [{'name': 'waiIllustriousSDXL_v170.safetensors', 'size': 6938040682}],
         'loras': [{'name': 'Iwao178.safetensors', 'size': 228000000}, {'name': 'akaimelon.safetensors', 'size': 228000001}]}


class Client:
    def list_model_files(self, folder):
        return FILES.get(folder, [])


cfg = load_config(sys.argv[1])
identity = {'character_tag': 'fern_(sousou_no_frieren)', 'lora': None}
fp = model_fingerprint(identity, cfg, Client())
portrait_identity = {'identity_block': 'sample', 'default_outfit_tags': [{'tag': 'coat'}]}
out = {'style_loras_in_config': [x['name'] for x in cfg['style_loras']],
       'tier_fingerprint': fp,
       'tier_cache_key': cache_key(fp),
       'portrait_cache_key': reference.cache_key(portrait_identity, cfg)}
print(json.dumps(out, ensure_ascii=False, indent=1))
