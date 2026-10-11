"""The WAI-list identity gate (V2-38; V2-43a: no character LoRA route; V2-43b: no tier, only the list)."""
from pathlib import Path

from storycg.identity import needs_user


def refuse_outside_wai_list(root,identity,config):
    """V2-38: only characters on the WAI list are supported. Stops in the identity stage: no probe, no prompt."""
    from storycg import wai_list
    return needs_user(root,identity,'character_not_in_wai_list',
                      question=wai_list.refusal_question(wai_list.settings_from(config)))


def wai_locked(identity, config=None):
    """V2-43b: the identity is locked and its character tag is on the WAI list (replaces the old L1/L2 check)."""
    from storycg import wai_list
    tag = identity.get('character_tag')
    return identity.get('status') == 'locked' and bool(tag) and wai_list.tier_row(tag, wai_list.settings_from(config)) is not None


def ensure_wai_identity(job_dir,identity,config):
    """The identity gate for every command. A locked character outside the WAI list stops here (needs_user,
    reason character_not_in_wai_list); a listed one keeps its WAI match evidence. No ComfyUI or vision call."""
    from storycg.identity_enrichment import outfit_hair_ornaments
    from storycg import wai_list
    from storycg.tiers import wai_list_result
    root = Path(job_dir).resolve()
    identity = outfit_hair_ornaments(identity)
    if identity.get('status') != 'locked': return identity
    tag = identity.get('character_tag')
    row = wai_list.tier_row(tag, wai_list.settings_from(config)) if tag else None
    if not row: return refuse_outside_wai_list(root, identity, config)
    return wai_list_result(root, identity, row)
