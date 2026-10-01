"""Deterministic tag-only prompts; human-readable state never enters conditioning."""
from storycg.config import load_config


def spaces(text):
    return text.replace('_', ' ')


def assemble_prompt(identity: dict, entry: dict, *, props=None, config=None) -> dict:
    config = config or load_config('config/default.json')
    identity_block = spaces(identity['identity_block'])
    outfit, scene, shot = entry['outfit'], entry['scene'], entry['shot']
    if 'pose' in shot:
        from storycg.shot_contract import moment
        moment(shot, '$.shot')
    registry = {p['id']: p for p in (props or [])}
    prop_blocks, bindings = [], []
    for ref in shot['key_props']:
        prop = registry[ref['prop_id']]
        description = ', '.join(spaces(t) for t in prop['description_tags'])
        relations = ', '.join(spaces(t) for t in [*ref['relation_tags'], *ref['location_tags']])
        # Keep each object's color/contact adjacent, rather than a global color soup.
        prop_blocks.append(', '.join(filter(None, [description, relations])))
        bindings.append({**ref, 'description_block': description, 'relation_block': relations})
    outfit_block = ', '.join(spaces(t) for t in outfit['tags'])
    scene_block = ', '.join(spaces(t) for t in scene['tags'])
    composition = [*config['framing_tags'][shot['framing']],
                   *config['camera_tags'][shot['camera_angle']]]
    # Model may redundantly repeat locked clothing/scene/object tags in shot.tags.
    # Keep the identity block intact, and keep each prop description intact and adjacent to contact.
    supplied = {spaces(t).strip() for t in identity['identity_block'].split(',')}
    supplied.update(spaces(t) for t in [*outfit['tags'], *scene['tags'], *composition])
    supplied.update(spaces(t) for p in (props or []) for t in p['description_tags'])
    action_tags = []
    for tag in shot['tags']:
        tag = spaces(tag)
        if tag not in supplied:
            action_tags.append(tag)
            supplied.add(tag)
    if shot.get('reflection') is True:
        action_tags.append('reflection')
    shot_block = ', '.join([*(spaces(t) for t in composition), *action_tags, *prop_blocks])
    return {'identity_block': identity_block, 'outfit_block': outfit_block,
            'scene_block': scene_block, 'shot_block': shot_block, 'prop_bindings': bindings,
            'positive_tags': ', '.join([identity_block, outfit_block, scene_block, shot_block]),
            'negative_tags': ', '.join(spaces(t) for t in [*scene['negative_tags'], *config.get('framing_negative_tags', {}).get(shot['framing'], [])]),
            'framing': shot['framing']}
