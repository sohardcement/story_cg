"""Generic static readability constraints, independent of story objects."""
import re

RULE_PROMPT = (
    'Declare subject_visibility clear or obscured, other_people restricted modes, hand_owners subject/anonymous_other. '
    'At most one shot may use behind camera or obscure the protagonist. Prefer front/side readable reactions. '
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
        shot=entry['shot'];tags=' '.join([*shot['tags'], *entry.get('scene', {}).get('tags', [])]).replace('_',' ').lower()
        if shot.get('subject_visibility')=='obscured' or shot['camera_angle']=='behind' or re.search(r'back view|from behind|covered face|hidden face|face out of frame',tags):obscured+=1
        if re.search(r'extra hands|other.+hand|disembodied hand|floating hand',tags+' '+shot['spatial_relation'].lower()) and 'hand_owner:' not in shot['spatial_relation']:
            raise StoryRuleViolation('hand_owner', [path+'.spatial_relation'], 'additional hand requires explicit owner', 'declare hand_owner: subject or anonymous other')
        # Archived holder records only identify whether an anonymous person is visible.
        legacy_other = any(ref.get('held_by')=='other' for ref in shot.get('key_props', []))
        anonymous_hand = 'anonymous_other' in shot.get('hand_owners', [])
        if (legacy_other and not anonymous_hand) or ((legacy_other or anonymous_hand) and not shot.get('other_people')):
            raise StoryRuleViolation('other_holder', [path+'.hand_owners', path+'.other_people'],
                'additional person requires explicit hand owner and restricted view',
                'add anonymous_other to hand_owners and a restricted other_people mode')
        if re.search(r'2girls|2boys|multiple girls|multiple boys',tags) and not re.search(r'back|passer|offscreen|cropped|partial',shot['spatial_relation'],re.I):
            raise StoryRuleViolation('restricted_people', [path+'.tags', path+'.spatial_relation'], 'other people must be anonymous restricted views', 'use back views, passerby, offscreen or cropped parts in spatial_relation')
    if not allow_obscured and obscured>max_obscured:raise StoryRuleViolation('obscured_limit', ['$.shots[*].shot.subject_visibility', '$.shots[*].shot.camera_angle', '$.shots[*].shot.tags'], 'too many obscured protagonist shots', f'keep at most {max_obscured} obscured shots; show clear front/side views in the others')
    return board
