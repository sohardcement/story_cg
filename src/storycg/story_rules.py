"""Generic static readability constraints, independent of story objects."""
import re

RULE_PROMPT = (
    'Declare subject_visibility clear or obscured, other_people restricted modes, hand_owners subject/anonymous_other, and each prop detached true/false. '
    'At most one shot may use behind camera or obscure the protagonist. Prefer front/side readable reactions. '
    'For a detached object event, depict the object already airborne, flying away or falling and separated from the subject; '
    'detached means an instant of departure through the air, not merely no longer held. Set detached=false for props placed/set down on a surface or at a resting location. '
    'held_by must be none, position MUST explicitly say separated from subject, and the subject must reach or turn. '
    'Other people only anonymous back views, passersby, offscreen or cropped parts. '
    'If any additional hand is depicted, put its owner in spatial_relation (hand_owner: subject or anonymous other). '
    'Never depict unexplained floating hands. Keep the main protagonist visible in most shots.'
)


class StoryRuleViolation(ValueError):
    """Stable rule identity and actionable field diagnostics for planner retries."""
    def __init__(self, rule_id, fields, problem, correction):
        self.rule_id = rule_id
        self.fields = fields
        super().__init__(f"{rule_id}: {', '.join(fields)}: {problem}. Fix: {correction}")


def validate_story_rules(board, *, allow_obscured=False, max_obscured=1):
    obscured=0
    for index, entry in enumerate(board['shots']):
        path = f'$.shots[{index}].shot'
        shot=entry['shot'];tags=' '.join(shot['tags']).replace('_',' ').lower()
        if shot.get('subject_visibility')=='obscured' or shot['camera_angle']=='behind' or re.search(r'back view|from behind|covered face|hidden face|face out of frame',tags):obscured+=1
        if re.search(r'extra hands|other.+hand|disembodied hand|floating hand',tags+' '+shot['spatial_relation'].lower()) and 'hand_owner:' not in shot['spatial_relation']:
            raise StoryRuleViolation('hand_owner', [path+'.spatial_relation'], 'additional hand requires explicit owner', 'declare hand_owner: subject or anonymous other')
        for prop_index, ref in enumerate(shot['key_props']):
            prop_path = f'{path}.key_props[{prop_index}]'
            visible=' '.join([ref['position'],*ref['location_tags']]).replace('_', ' ').lower()
            departing = re.search(r'\b(?:airborne|in (?:the )?air|floating|blown away|flying away|falling|leaving (?:the )?subject)\b', visible)
            resting = re.search(r'\b(?:placed|set down|resting|on (?:a |the )?surface)\b', visible) or re.search(r'(?:^|[,;]\s*)on\s+\w+', ref['position'], re.I)
            if ref.get('held_by')=='other' and ('anonymous_other' not in shot.get('hand_owners',[]) or not shot.get('other_people')):
                raise StoryRuleViolation('other_holder', [prop_path+'.held_by', path+'.hand_owners', path+'.other_people'], 'other person holding object requires explicit hand owner and restricted view', 'add anonymous_other to hand_owners and a restricted other_people mode')
            if departing or (ref.get('detached') and not resting):
                if ref['held_by']!='none' or not re.search(r'separat|detached from',ref['position'],re.I):
                    raise StoryRuleViolation('detached_separation', [prop_path+'.held_by', prop_path+'.position'], 'detached prop must be separated from subject with held_by none', 'set held_by=none and describe airborne separation explicitly in position; for a resting placed prop set detached=false and describe its resting location')
                if not re.search(r'reach|turn|look back|chase|run',tags+' '+shot['action'].lower()):
                    raise StoryRuleViolation('detached_reaction', [path+'.action', path+'.tags', prop_path+'.detached', prop_path+'.position'], 'detached moment requires reaching/turning reaction', 'show reach/turn/chase in action and real pose tags for departure; for placement set detached=false and describe the resting position')
        if re.search(r'2girls|2boys|multiple girls|multiple boys',tags) and not re.search(r'back|passer|offscreen|cropped|partial',shot['spatial_relation'],re.I):
            raise StoryRuleViolation('restricted_people', [path+'.tags', path+'.spatial_relation'], 'other people must be anonymous restricted views', 'use back views, passerby, offscreen or cropped parts in spatial_relation')
    if not allow_obscured and obscured>max_obscured:raise StoryRuleViolation('obscured_limit', ['$.shots[*].shot.subject_visibility', '$.shots[*].shot.camera_angle', '$.shots[*].shot.tags'], 'too many obscured protagonist shots', f'keep at most {max_obscured} obscured shots; show clear front/side views in the others')
    return board
