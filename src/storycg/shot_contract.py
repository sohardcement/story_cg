"""Atomic person moment contract with background-only object tags.

Version 1 is read-only compatibility for archived boards. New planning uses v2.
No action vocabulary or story/character-specific branches are used.
"""
import re
from storycg.story_rules import StoryRuleViolation

EXPRESSION_TAGS=('expressionless','smile','frown','blush','surprised','confused','embarrassed','angry','sad','tears','pout','grin','serious')

ATOMIC_TAG = {'type':'string','pattern':r'^[a-z0-9]+(?:_[a-z0-9]+)*$','minLength':1}


def extend_schema(schema, *, required=False):
    schema['properties']['schema_version']={'const':2}
    schema['properties'].update(scope_version={'enum':[1, 2]}, story_note={'type':'string'})
    shot=schema['properties']['shots']['items']['properties']['shot']
    shot['properties']['unchanged_person_reason']={'type':'string'}
    shot['properties'].update(pose=ATOMIC_TAG, protagonist_instances={'type':'integer','minimum':1,'maximum':2},
                              pose_tag_omitted={'type':'boolean'})
    if required:
        schema['required'].append('schema_version')
        shot['required']+=['pose','reflection','protagonist_instances']
        # There is just one conditioning moment; prose cannot hide a second pose.
        for key in ('action','description','expression'):shot['properties'][key]=ATOMIC_TAG
        shot['properties']['expression']={'enum':list(EXPRESSION_TAGS)}
        shot['properties']['tags']['maxItems']=2
    return schema


def moment(shot, path):
    for field in ('pose','action','description','expression'):
        value=shot.get(field)
        if not isinstance(value,str) or not re.fullmatch(ATOMIC_TAG['pattern'],value):
            raise StoryRuleViolation('single_moment',[path+'.'+field],
                'requires one atomic tag, not prose, a list or a sequence', 'use one verified tag for the current instant')
    for field in ('action','description'):
        if shot[field]!=shot['pose']:
            raise StoryRuleViolation('single_moment',[path+'.'+field,path+'.pose'],
                'must name the same single instant', 'copy pose exactly; place the next instant in another shot')
    omitted=shot.get('pose_tag_omitted',False)
    allowed={shot['expression']} if omitted else {shot['pose'],shot['expression']}
    if not shot.get('tags') or any(t not in allowed for t in shot['tags']) or (not omitted and shot['pose'] not in shot['tags']):
        raise StoryRuleViolation('single_moment',[path+'.tags',path+'.pose',path+'.expression'],
            'conditioning may contain only the one pose and expression', 'use pose and expression; background objects belong in prop_tags')
    count=shot.get('protagonist_instances')
    if type(shot.get('reflection')) is not bool or type(count) is not int or count!=1+int(shot.get('reflection',False)):
        raise StoryRuleViolation('single_protagonist',[path+'.protagonist_instances',path+'.reflection'],
            'one protagonist instance; a second is allowed only as an explicit reflection', 'declare reflection boolean and count 1, or 2 for the one reflection')


def validate_contract(board):
    if board.get('schema_version')!=2:return board
    from storycg.posture_tags import validate_posture_blocks
    from storycg.tags import canonical_tag
    validate_posture_blocks(board)
    for i,entry in enumerate(board['shots']):
        path=f'$.shots[{i}].shot';shot=entry['shot'];moment(shot,path)
        if any(canonical_tag(tag) in EXPRESSION_TAGS for tag in shot.get('prop_tags', [])):
            raise StoryRuleViolation('single_moment', [path+'.prop_tags'],
                'background object tags cannot supply an expression',
                'keep the one expression in shot.expression')
    return board

RULE_PROMPT=(
    ' D16: schema_version=2. Every shot depicts exactly ONE instantaneous pose/action. '
    'pose is ONE real atomic Danbooru tag, not a phrase/list/sequence. The script derives action, description and shot.tags from pose/expression; do not return these derived fields. '
    'expression is ONE real emotion tag, never another action or posture. Never put another pose in any tag block. '
    'Background objects go in prop_tags as ordinary tags. Split sequential actions into separate shots. '
    'Always declare reflection=false and protagonist_instances=1, or reflection=true and protagonist_instances=2 for exactly one reflection of that SAME instant. '
)


def planner_schema(schema):
    """Remove redundant writable representations of the same moment."""
    import copy
    schema=copy.deepcopy(schema)
    # Opaque props/key_props exist only for archived readers in the remaining layers.
    schema['required'].remove('props')
    del schema['properties']['props']
    schema['properties']['scope_version']={'const':2}
    for field in ('scope_version','story_note'):
        if field not in schema['required']:schema['required'].append(field)
    schema['properties']['shots']['items']['properties']['shot']['required'].append('unchanged_person_reason')
    shot=schema['properties']['shots']['items']['properties']['shot']
    shot['required'].remove('key_props')
    del shot['properties']['key_props']
    shot['required'].append('prop_tags')
    shot['properties']['prop_tags']={'type':'array','maxItems':1,'items':ATOMIC_TAG}
    # Only exact registry validation may mark an action as unconditioned.
    del shot['properties']['pose_tag_omitted']
    for field in ('description','action','tags'):
        shot['required'].remove(field)
        del shot['properties'][field]
    return schema


def materialize(board):
    """Compile the single structured moment, without rewriting a supplied pose."""
    import copy
    compiled=copy.deepcopy(board)
    compiled['props']=[]
    for entry in compiled['shots']:
        shot=entry['shot']
        if 'pose_tag_omitted' in shot:raise ValueError('planner supplied derived field: shot.pose_tag_omitted')
        for field in ('action','description','tags'):
            if field in shot:raise ValueError('planner supplied derived field: shot.'+field)
        shot.update(action=shot['pose'],description=shot['pose'],
                    tags=list(dict.fromkeys([shot['pose'],shot['expression']])))
        shot['key_props']=[]
        entry['scene']['tags']=list(dict.fromkeys([*entry['scene']['tags'], *shot['prop_tags']]))
    return compiled
