"""Generic static readability constraints, independent of story objects."""
import re

RULE_PROMPT = (
    'Declare subject_visibility clear or obscured, other_people restricted modes, hand_owners subject/anonymous_other, and each prop detached true/false. '
    'At most one shot may use behind camera or obscure the protagonist. Prefer front/side readable reactions. '
    'For a detached object event, depict the object already airborne/detached and separated from the subject; '
    'held_by must be none, position MUST explicitly say separated from subject, and the subject must reach or turn. '
    'Other people only anonymous back views, passersby, offscreen or cropped parts. '
    'If any additional hand is depicted, put its owner in spatial_relation (hand_owner: subject or anonymous other). '
    'Never depict unexplained floating hands. Keep the main protagonist visible in most shots.'
)


def validate_story_rules(board, *, allow_obscured=False, max_obscured=1):
    obscured=0
    for entry in board['shots']:
        shot=entry['shot'];tags=' '.join(shot['tags']).replace('_',' ').lower()
        if shot.get('subject_visibility')=='obscured' or shot['camera_angle']=='behind' or re.search(r'back view|from behind|covered face|hidden face|face out of frame',tags):obscured+=1
        if re.search(r'extra hands|other.+hand|disembodied hand|floating hand',tags+' '+shot['spatial_relation'].lower()) and 'hand_owner:' not in shot['spatial_relation']:
            raise ValueError('additional hand requires explicit owner')
        for ref in shot['key_props']:
            visible=' '.join([ref['position'],*ref['location_tags']]).lower()
            if ref.get('held_by')=='other' and ('anonymous_other' not in shot.get('hand_owners',[]) or not shot.get('other_people')):
                raise ValueError('other person holding object requires explicit hand owner and restricted view')
            if ref.get('detached') or re.search(r'airborne|in air|floating|detached|blown away|falling',visible):
                if ref['held_by']!='none' or not re.search(r'separat|detached from',ref['position'],re.I):
                    raise ValueError('detached prop must be separated from subject with held_by none')
                if not re.search(r'reach|turn|look back|chase|run',tags+' '+shot['action'].lower()):
                    raise ValueError('detached moment requires reaching/turning reaction')
        if re.search(r'2girls|2boys|multiple girls|multiple boys',tags) and not re.search(r'back|passer|offscreen|cropped|partial',shot['spatial_relation'],re.I):
            raise ValueError('other people must be anonymous restricted views')
    if not allow_obscured and obscured>max_obscured:raise ValueError('too many obscured protagonist shots')
    return board
