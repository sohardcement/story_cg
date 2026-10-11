"""Generation identity settings and the WAI-list identity result (V2-43a: the character LoRA route is removed)."""
import copy
from pathlib import Path

from storycg.identity_enrichment import load_rules
from storycg.state import save_json_atomic


def tier_generation_config(config):
    cfg=copy.deepcopy(config)
    cfg.pop('generation_reference',None)
    cfg['identity_control']={**cfg.get('identity_control',{}),**copy.deepcopy(load_rules()['generation_identity_control'])}
    return cfg


def wai_list_result(root, identity, row):
    """V2-25 / V2-38: a character on the WAI list is identified from its tag alone. No probe image is made.
    V2-43b: the match evidence is kept as wai_list_match; no tier field is written."""
    result=copy.deepcopy(identity)
    for key in ('reason','question','user_can_provide'): result.pop(key,None)
    result['wai_list_match']=row
    save_json_atomic(Path(root)/'identity.json',result)
    return result
