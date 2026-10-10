"""Deterministic tag-only prompts; human-readable state never enters conditioning."""
from storycg.config import load_config
from storycg.framing import framing_rule
from storycg.prompt_rules import (companion_tags_for_shot, filter_scene_tags,
                                  indoor_precipitation_negatives, negative_tag_list)


def spaces(text):
    return text.replace('_', ' ')


def full_negative_prompt(config, parts):
    """The negative prompt a shot is submitted with: config negative, then the assembled shot negatives.

    Every candidate of a shot is submitted with it (candidates.run_candidate_delivery).
    """
    return ", ".join(filter(None, [config["negative_prompt"], parts["negative_tags"]]))


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
    if config.get('scene_rules'):
        kept_scene, dropped_scene = filter_scene_tags(shot['framing'], scene['tags'],
                                                      shot.get('prop_tags', []), config)
    else:
        kept_scene, dropped_scene = scene['tags'], []
    scene_block = ', '.join(spaces(t) for t in kept_scene)
    composition = [*framing_rule(config, shot['framing'])['tags'],
                   *config['camera_tags'][shot['camera_angle']]]
    # Keep the identity block intact; avoid repeated clothing, scene and framing tags.
    supplied = {spaces(t).strip() for t in identity['identity_block'].split(',')}
    supplied.update(spaces(t) for t in [*outfit['tags'], *kept_scene, *composition])
    action_tags = []
    for tag in shot['tags']:
        tag = spaces(tag)
        if tag not in supplied:
            action_tags.append(tag)
            supplied.add(tag)
    for tag in companion_tags_for_shot(shot, config):
        tag = spaces(tag)
        if tag not in supplied:
            action_tags.append(tag)
            supplied.add(tag)
    if shot.get('reflection') is True:
        action_tags.append('reflection')
    shot_block = ', '.join([*(spaces(t) for t in composition), *action_tags])
    negative_tags = negative_tag_list(scene['negative_tags'],
                                      config.get('framing_negative_tags', {}).get(shot['framing'], []),
                                      config)
    positive_tags = ', '.join([identity_block, outfit_block, scene_block, shot_block])
    # V2-18 (D60): indoor precipitation adds ground-water negatives (same trigger as the V2-11 window).
    indoor_negative = indoor_precipitation_negatives(scene['tags'], positive_tags, negative_tags, config)
    negative_tags = [*negative_tags, *indoor_negative['added']]
    return {'identity_block': identity_block, 'outfit_block': outfit_block,
            'scene_block': scene_block, 'shot_block': shot_block,
            'positive_tags': positive_tags,
            'negative_tags': ', '.join(spaces(t) for t in negative_tags),
            'dropped_scene_tags': dropped_scene, 'framing': shot['framing'],
            'indoor_precipitation_negative': indoor_negative}
