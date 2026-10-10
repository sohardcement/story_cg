"""D18 tier aggregation over frozen comparison observations, and the identity-tier entry point (V2-38: WAI list only)."""
import copy
import json

import httpx
from pathlib import Path
from storycg import tiers as frozen
from storycg.config import character_lora_matching_enabled
from storycg.identity import needs_user
from storycg.state import save_json_atomic


def evaluate_l2(checklist,comparisons,reliability):
    raw=frozen.evaluate_l2(checklist,comparisons,reliability)
    gate=copy.deepcopy(raw)
    def can_veto(item):
        if item in ('accessories','hair_ornaments'):return False
        policy=reliability.get('items',{}).get(item,{})
        return policy.get('status')=='reliable' or (policy.get('status')=='unreliable' and policy.get('false_positive_rate')==0)
    gate['vetoes']=[x for x in raw['vetoes'] if can_veto(x['item'])]
    gate['ignored_vetoes']=[x for x in comparisons if x.get('item')!='outfit'
        and x.get('status')=='fail' and x.get('applicable',True) and not can_veto(x['item'])]
    gate['reliable']=[x for x in gate['reliable'] if x['item'] not in ('accessories','hair_ornaments')]
    gate['identity_tier']='L2' if gate['reliable'] and all(x['passed'] for x in gate['reliable']) and not gate['vetoes'] else 'tier_pending_L1'
    gate['raw_aggregation']=raw
    gate['limitations']='D20: accessories/hair_ornaments cannot veto or confirm; D18: unverified cannot veto; unreliable with calibrated zero false positives may veto; all reliable appearance items must pass; outfit diagnostic for L2'
    return gate


def aggregate_comparison(root,identity,raw,reliability,*,character_lora=None,artifact='tier_gate.json'):
    from storycg.tiers import comparison_available
    if not comparison_available(raw):return raw
    rows=[row for pair in raw['tier_evidence']['comparisons'] for row in pair.get('items',[])]
    gate=evaluate_l2(frozen.identity_checklist(identity),rows,reliability)
    if character_lora and gate['identity_tier']=='L2':gate['identity_tier']='L1'
    output=copy.deepcopy(raw)
    output['identity_tier']=gate['identity_tier']
    output['tier_gate']=gate
    output['tier_evidence'].update({k:gate[k] for k in ('reliable','unverified','outfit_comparisons','accessory_comparisons','vetoes','not_visible','limitations')})
    output['tier_evidence']['tier_gate_policy']='D18/D20'
    save_json_atomic(Path(root)/artifact,{'policy':'D18/D20','raw_result':raw,'gate':gate})
    return output


def compare_l2(root,identity,tests,references,reliability,vlm,**kwargs):
    raw=frozen.compare_l2(root,identity,tests,references,reliability,vlm,**kwargs)
    return aggregate_comparison(root,identity,raw,reliability,character_lora=kwargs.get('character_lora'),
        artifact=kwargs.get('comparison_file','l2/comparison-v3.json')+'.gate.json')


def _determine_identity_tier(job_dir,identity,config,client,**kwargs):
    """The L1 route (character LoRA matching on): the probe, then the D18 gate. Anything short of L1 stops."""
    root=Path(job_dir).resolve()
    result=frozen.ensure_identity_tier(root,identity,config,client,**kwargs)
    if result.get('identity_tier')!='tier_pending_L1':return result
    reliability=json.loads(Path('config/review_reliability.json').read_text())
    evidence=result.get('tier_evidence',{})
    raw={'identity_tier':result['identity_tier'],'tier_evidence':evidence}
    gated=aggregate_comparison(root,result,raw,reliability,character_lora=result.get('lora'))
    approved=copy.deepcopy(result)
    approved['identity_tier']=gated['identity_tier'];approved['tier_evidence']=gated['tier_evidence']
    save_json_atomic(root/'identity.json',approved)
    if approved['identity_tier']=='L1':return approved
    return needs_user(root,approved,'l1_validation_failed',question=
        '角色 LoRA 的身份验证未通过：底模生成的角色外貌与来源资料对不上，无法确认底模认识该角色。'
        '请换一个清单里的角色，或换一个可用的角色 LoRA 后重试。')


def refuse_outside_wai_list(root,identity,config):
    """V2-38: only characters on the WAI list are supported. Stops in the identity stage: no probe, no prompt."""
    from storycg import wai_list
    return needs_user(root,identity,'character_not_in_wai_list',
                      question=wai_list.refusal_question(wai_list.settings_from(config)))


def ensure_identity_tier(job_dir,identity,config,client,**kwargs):
    """The identity-tier entry point for every command. A locked character outside the WAI list stops here
    (needs_user, reason character_not_in_wai_list); a listed one is L2 from its tag, or L1 with the LoRA switch on."""
    from storycg.tier_cache import (ModelIdentityUnavailable, model_fingerprint,
        cached_tier, store_tier, reuse_tier)
    from storycg.identity_enrichment import outfit_hair_ornaments
    from storycg import wai_list
    root = Path(job_dir).resolve()
    identity = outfit_hair_ornaments(identity)
    if identity.get('status') != 'locked': return identity
    tag = identity.get('character_tag')
    settings = wai_list.settings_from(config)
    row = wai_list.tier_row(tag, settings) if tag else None
    if not row: return refuse_outside_wai_list(root, identity, config)
    if not character_lora_matching_enabled(config):
        from storycg.tiers import wai_list_result
        return wai_list_result(root, identity, row)
    if not config.get('tier_cache'):
        return _determine_identity_tier(root, identity, config, client, **kwargs)
    try:
        # Establish the checkpoint identity before doing extra inventory work.
        model_fingerprint({**identity, 'lora': None}, config, client)
        inventory = kwargs.get('inventory')
        if inventory is None:
            from storycg.identity import BASE_URL, USER_AGENT
            reference = kwargs.get('reference_client')
            if reference is None:
                with httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False,
                                  headers={'User-Agent': USER_AGENT}) as owned:
                    inventory = frozen.live_inventory(config, client, owned, root)
            else:
                inventory = frozen.live_inventory(config, client, reference, root)
        kwargs = {**kwargs, 'inventory': inventory}
        selected = frozen.match_character_loras(identity['character_tag'], inventory['names'],
                inventory['metadata'], character_tags=inventory['character_tags'])['selected']
        current = {**identity, 'lora': selected}
        fingerprint = model_fingerprint(current, config, client)
    except ModelIdentityUnavailable as exc:
        save_json_atomic(root/'tier/cache-status.json', {'status': 'unavailable', 'reason': str(exc)})
        if identity.get('tier_model_fingerprint'):
            result = copy.deepcopy(identity)
            result.update(identity_tier='tier_pending_resources', status='locked')
            result['tier_cache'] = {'hit': False, 'status': 'unavailable', 'reason': str(exc)}
            save_json_atomic(root/'identity.json', result)
            return result
        return _determine_identity_tier(root, identity, config, client, **kwargs)
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        result = copy.deepcopy(identity)
        result.update(identity_tier='tier_pending_resources', status='locked')
        result['tier_cache'] = {'hit': False, 'status': 'unavailable', 'reason': str(exc)}
        save_json_atomic(root/'identity.json', result)
        return result
    record = cached_tier(config, fingerprint)
    if record: return reuse_tier(root, current, record)
    save_json_atomic(root/'tier/cache-reuse.json', {'hit': False, 'key_inputs': fingerprint})
    identity = copy.deepcopy(current)
    previous = identity.get('tier_model_fingerprint')
    if previous != fingerprint:
        from storycg.tier_cache import cache_key
        save_json_atomic(root/'tier/model-history'/(cache_key(previous or {})+'.json'), identity)
        identity['tier_evidence'] = {}
        identity.update(identity_tier='tier_pending_comfyui', status='locked', needs_user=False)
        for field in ('reason', 'question', 'user_can_provide', 'tier_cache'): identity.pop(field, None)
    identity['tier_model_fingerprint'] = fingerprint
    result = _determine_identity_tier(root, identity, config, client, **kwargs)
    result['tier_model_fingerprint'] = fingerprint
    save_json_atomic(root/'identity.json', result)
    stored = store_tier(root, result, config, fingerprint)
    if stored and stored['source_job'] != str(root): return reuse_tier(root, result, stored)
    return result
