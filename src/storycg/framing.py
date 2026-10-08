"""One framing rule supplies both conditioning tags and observable body limits."""
from storycg.tags import canonical_tag


def framing_rule(config, framing):
    if 'framing' in config:
        return config['framing'].get(framing, {})
    # Saved v1 task configurations remain readable; new configurations use one table.
    return {'tags': config.get('framing_tags', {}).get(framing, []),
            'lowest_visible': config.get('framing_definitions', {}).get(framing, [])}


def allowed_parts(config, framing, pose=None):
    rule = framing_rule(config, framing)
    allowed = list(rule.get('lowest_visible', []))
    groups = config.get('framing_postures', {}).get('groups', {})
    compact = {tag for tags in groups.values() for tag in tags}
    poses = [pose] if isinstance(pose, str) else pose or []
    if any(canonical_tag(tag) in compact for tag in poses):
        allowed = list(rule.get('compact_posture_lowest_visible', allowed))
    return allowed
