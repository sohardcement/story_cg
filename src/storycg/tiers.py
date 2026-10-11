"""Generation identity settings and the WAI-list identity result (V2-43a: the character LoRA route is removed)."""
import copy
from pathlib import Path

from storycg.identity_enrichment import load_rules
from storycg.state import save_json_atomic


def tier_generation_config(config,identity):
    tier=identity.get('identity_tier','pending')
    if tier=='L1':
        raise ValueError('L1 (character LoRA) route removed in V2-43a; create a new job')
    if tier!='L2':
        raise ValueError(f'identity tier pending: {tier}; no story generation')
    cfg=copy.deepcopy(config)
    cfg.pop('generation_reference',None)
    cfg['identity_control']={**cfg.get('identity_control',{}),**copy.deepcopy(load_rules()['generation_identity_control'])}
    return cfg


def wai_list_result(root, identity, row):
    """V2-25 / V2-38: a character on the WAI list is L2 from its tag alone. No probe image is made."""
    result=copy.deepcopy(identity)
    for key in ('reason','question','user_can_provide'): result.pop(key,None)
    result.update(identity_tier='L2',tier_evidence={'status':'wai_list',
        'reason':'角色在 WAI 清单内，只靠 tag 可画出，直接判 L2，不出探针图',
        'generation_count':0,'vision_calls':0,'wai_list':row})
    save_json_atomic(Path(root)/'identity.json',result)
    return result
