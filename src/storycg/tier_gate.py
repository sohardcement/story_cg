"""D18 tier aggregation over immutable frozen comparison observations."""
import copy
import json
import httpx
from pathlib import Path
from storycg import tiers as frozen
from storycg.config import character_lora_matching_enabled
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
    gate['identity_tier']='L2' if gate['reliable'] and all(x['passed'] for x in gate['reliable']) and not gate['vetoes'] else 'tier_pending_L3_L4'
    gate['raw_aggregation']=raw
    gate['limitations']='D20: accessories/hair_ornaments cannot veto or confirm; D18: unverified cannot veto; unreliable with calibrated zero false positives may veto; all reliable appearance items must pass; outfit diagnostic for L2'
    return gate


def aggregate_comparison(root,identity,raw,reliability,*,character_lora=None,artifact='tier_gate.json'):
    from storycg.fallback import comparison_available
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
    root=Path(job_dir).resolve()
    # Freeze handles provenance, generation and original audits; defer L3 until D18 aggregation.
    supplied=dict(kwargs);allow_l3=supplied.pop('allow_l3',True)
    result=frozen.ensure_identity_tier(root,identity,config,client,allow_l3=False,**supplied)
    if result.get('identity_tier')!='tier_pending_L3_L4':return result
    reliability=json.loads(Path('config/review_reliability.json').read_text())
    evidence=result.get('tier_evidence',{})
    raw={'identity_tier':result['identity_tier'],'tier_evidence':evidence}
    gated=aggregate_comparison(root,result,raw,reliability,character_lora=result.get('lora'))
    approved=copy.deepcopy(result)
    approved['identity_tier']=gated['identity_tier'];approved['tier_evidence']=gated['tier_evidence']
    save_json_atomic(root/'identity.json',approved)
    if approved['identity_tier'] in ('L1','L2') or not allow_l3:return approved
    from storycg.fallback import ensure_l3,import_user_reference
    opts=json.loads((root/'job.json').read_text()).get('generation_options',{})
    user_ref=import_user_reference(root,opts['reference_path']) if opts.get('reference_path') else None
    probe=root/evidence['artifact_root']
    refs=[user_ref] if user_ref else json.loads((probe/frozen.load_rules()['references']['directory']/'manifest.json').read_text())
    return ensure_l3(root,approved,config,client,references=refs,reference_root=root if user_ref else probe,
                     vlm=supplied.get('vlm'),force_probe=supplied.get('force_probe',False))


def ensure_identity_tier(job_dir,identity,config,client,**kwargs):
    """Reuse an evidence-backed tier keyed by the reported model identity."""
    from storycg.tier_cache import (ModelIdentityUnavailable, model_fingerprint,
        cached_tier, store_tier, reuse_tier)
    from storycg.identity_enrichment import outfit_hair_ornaments
    root = Path(job_dir).resolve()
    identity = outfit_hair_ornaments(identity)
    from storycg import wai_list
    if not character_lora_matching_enabled(config) and identity.get('character_tag'):
        # V2-25: a character on the WAI list is drawable from its tag alone: L2, no probe image.
        row=wai_list.tier_row(identity['character_tag'],wai_list.settings_from(config))
        if row:
            result=copy.deepcopy(identity)
            for key in ('reason','question','user_can_provide'): result.pop(key,None)
            result.update(identity_tier='L2',tier_evidence={'status':'wai_list',
                'reason':'角色在 WAI 清单内，只靠 tag 可画出，直接判 L2，不出探针图',
                'generation_count':0,'vision_calls':0,'wai_list':row})
            save_json_atomic(root/'identity.json',result)
            return result
    if not config.get('tier_cache') or not identity.get('character_tag'):
        return _determine_identity_tier(root, identity, config, client, **kwargs)
    try:
        # Establish the checkpoint identity before doing extra inventory work.
        model_fingerprint({**identity, 'lora': None}, config, client)
        if character_lora_matching_enabled(config):
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
        else:
            # V2-20: no LoRA inventory is read and no character LoRA is selected.
            selected = None
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
    result = _determine_identity_tier(root, identity, config, client,
                **{**kwargs, 'force_probe': previous != fingerprint})
    result['tier_model_fingerprint'] = fingerprint
    save_json_atomic(root/'identity.json', result)
    stored = store_tier(root, result, config, fingerprint)
    if stored and stored['source_job'] != str(root): return reuse_tier(root, result, stored)
    return result
