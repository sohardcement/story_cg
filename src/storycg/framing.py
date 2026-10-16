"""One framing rule supplies both conditioning tags and observable body limits."""
from storycg.tags import canonical_tag


def framing_rule(config, framing):
    if 'framing' in config:
        return config['framing'].get(framing, {})
    # Saved v1 task configurations remain readable; new configurations use one table.
    return {'tags': config.get('framing_tags', {}).get(framing, []),
            'lowest_visible': config.get('framing_definitions', {}).get(framing, [])}
