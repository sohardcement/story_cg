"""Deterministic tag-only prompts; human-readable state never enters conditioning."""
from storycg.config import load_config
from storycg.framing import framing_rule


def spaces(text):
    return text.replace('_', ' ')


def assemble_prompt(identity: dict, entry: dict, *, props=None, config=None) -> dict:
    config = config or load_config('config/default.json')
    if 'pose' in entry['shot']:
        from storycg.posture_tags import filtered_inputs
        entry,props=filtered_inputs(entry,props)
    identity_block = spaces(identity['identity_block'])
    outfit, scene, shot = entry['outfit'], entry['scene'], entry['shot']
    if 'pose' in shot:
        from storycg.shot_contract import moment
        moment(shot, '$.shot')
    outfit_block = ', '.join(spaces(t) for t in outfit['tags'])
    scene_block = ', '.join(spaces(t) for t in scene['tags'])
    composition = [*framing_rule(config, shot['framing'])['tags'],
                   *config['camera_tags'][shot['camera_angle']]]
    # Keep the identity block intact; avoid repeated clothing, scene and framing tags.
    supplied = {spaces(t).strip() for t in identity['identity_block'].split(',')}
    supplied.update(spaces(t) for t in [*outfit['tags'], *scene['tags'], *composition])
    action_tags = []
    for tag in shot['tags']:
        tag = spaces(tag)
        if tag not in supplied:
            action_tags.append(tag)
            supplied.add(tag)
    if shot.get('reflection') is True:
        action_tags.append('reflection')
    shot_block = ', '.join([*(spaces(t) for t in composition), *action_tags])
    return {'identity_block': identity_block, 'outfit_block': outfit_block,
            'scene_block': scene_block, 'shot_block': shot_block,
            'positive_tags': ', '.join([identity_block, outfit_block, scene_block, shot_block]),
            'negative_tags': ', '.join(spaces(t) for t in [*scene['negative_tags'], *config.get('framing_negative_tags', {}).get(shot['framing'], [])]),
            'framing': shot['framing']}
