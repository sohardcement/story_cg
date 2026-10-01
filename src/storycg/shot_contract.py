"""D16 contract: one atomic moment and ownership evidence before comparison.

Version 1 is read-only compatibility for archived boards. New planning uses v2.
No action vocabulary or story/character-specific branches are used.
"""
import re
from collections import defaultdict
from storycg.story_rules import StoryRuleViolation

EXPRESSION_TAGS=('expressionless','smile','frown','blush','surprised','confused','embarrassed','angry','sad','tears','pout','grin','serious')

ATOMIC_TAG = {'type':'string','pattern':r'^[a-z0-9]+(?:_[a-z0-9]+)*$','minLength':1}


def extend_schema(schema, *, required=False):
    schema['properties']['schema_version']={'const':2}
    shot=schema['properties']['shots']['items']['properties']['shot']
    shot['properties'].update(pose=ATOMIC_TAG, protagonist_instances={'type':'integer','minimum':1,'maximum':2})
    prop=schema['properties']['props']['items']
    prop['properties'].update(owner={'type':'string','minLength':1},distinguish_ownership={'type':'boolean'})
    ref=shot['properties']['key_props']['items']
    ref['properties'].update(establishes_ownership={'type':'boolean'},distinguishing_features_visible={'type':'boolean'})
    if required:
        schema['required'].append('schema_version')
        shot['required']+=['pose','reflection','protagonist_instances']
        # There is just one conditioning moment; prose cannot hide a second pose.
        for key in ('action','description','expression'):shot['properties'][key]=ATOMIC_TAG
        shot['properties']['expression']={'enum':list(EXPRESSION_TAGS)}
        shot['properties']['tags']['maxItems']=2
        prop['required']+=['owner','distinguish_ownership']
        ref['required']+=['establishes_ownership','distinguishing_features_visible']
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
    allowed={shot['pose'],shot['expression']}
    if not shot.get('tags') or any(t not in allowed for t in shot['tags']) or shot['pose'] not in shot['tags']:
        raise StoryRuleViolation('single_moment',[path+'.tags',path+'.pose',path+'.expression'],
            'conditioning may contain only the one pose and expression', 'use pose and expression; object contact belongs in key_props')
    count=shot.get('protagonist_instances')
    if type(shot.get('reflection')) is not bool or type(count) is not int or count!=1+int(shot.get('reflection',False)):
        raise StoryRuleViolation('single_protagonist',[path+'.protagonist_instances',path+'.reflection'],
            'one protagonist instance; a second is allowed only as an explicit reflection', 'declare reflection boolean and count 1, or 2 for the one reflection')


def validate_contract(board):
    if board.get('schema_version')!=2:return board
    registry={p['id']:p for p in board['props']}
    groups=defaultdict(list)
    for p in board['props']:
        if p.get('role','key')=='key':groups[p['kind'].strip().lower().replace(' ','_')].append(p)
    ownership_groups={kind:ps for kind,ps in groups.items() if len(ps)>1 and (
        len({p.get('owner','none') for p in ps})>1 or any(p.get('distinguish_ownership') for p in ps))}
    attributes=re.compile(r'(?:^|_)(red|blue|green|yellow|orange|purple|pink|white|black|grey|gray|brown|silver|gold|multicolored|rainbow|striped|plaid|checkered|polka_dot|floral|argyle|camouflage|patterned)(?:_|$)')
    for kind,ps in ownership_groups.items():
        signatures=[frozenset(m.group(1) for tag in p['description_tags'] for m in attributes.finditer(tag.replace(' ','_'))) for p in ps]
        if any(not v for v in signatures) or len(set(signatures))!=len(signatures):
            raise StoryRuleViolation('ownership',['$.props'], 'ownership-distinguished objects need distinct visible color/pattern signatures', 'register conspicuously different color/pattern tags for each individually tracked object')
    established={}; seen=defaultdict(set)
    for i,entry in enumerate(board['shots']):
        path=f'$.shots[{i}].shot';shot=entry['shot'];moment(shot,path)
        refs=shot['key_props'];current=defaultdict(set);new_evidence=[]
        for j,r in enumerate(refs):
            p=registry.get(r['prop_id'])
            if p is None:continue  # Base validator supplies the unknown-ref feedback.
            rp=f'{path}.key_props[{j}]';kind=p['kind'].strip().lower().replace(' ','_')
            if r.get('owner')!=p.get('owner'):
                raise StoryRuleViolation('ownership',[rp+'.owner'], 'owner differs from registered immutable owner', 'use the registered owner, independently of holder')
            if r['held_by']!='offscreen':current[kind].add(p['id'])
            if r.get('establishes_ownership'):
                if p.get('owner')!='subject' or r['held_by']!='subject' or not r.get('distinguishing_features_visible') or shot.get('subject_visibility')!='clear' or not r['relation_tags']:
                    raise StoryRuleViolation('ownership',[rp+'.establishes_ownership',rp+'.distinguishing_features_visible',rp+'.held_by'],
                        'ownership evidence needs the visible subject holding their own distinguishable object', 'show contact and visible distinguishing features in an earlier shot')
                new_evidence.append(kind)
        for kind,ps in ownership_groups.items():
            if len(seen[kind]|current[kind])>=2 and current[kind] and kind not in established:
                raise StoryRuleViolation('ownership',[path+'.key_props','$.props'],
                    'same-kind ownership comparison appears before subject ownership is established', 'add an earlier shot holding the subject-owned object with distinguishing features clearly visible')
        for kind in new_evidence:established.setdefault(kind,i)
        for kind,ids in current.items():seen[kind].update(ids)
    return board

RULE_PROMPT=(
    ' D16: schema_version=2. Every shot depicts exactly ONE instantaneous pose/action. '
    'pose is ONE real atomic Danbooru tag, not a phrase/list/sequence. The script derives action, description and shot.tags from pose/expression; do not return these derived fields. '
    'expression is ONE real emotion tag, never another action or posture. Never put another pose in any tag block. '
    'Object contact and location go in key_props relation_tags/location_tags. Split sequential actions into separate shots. '
    'Always declare reflection=false and protagonist_instances=1, or reflection=true and protagonist_instances=2 for exactly one reflection of that SAME instant. '
    'Every registered prop has a stable owner (subject/other/none or anonymous owner id), independent of who holds it, '
    'and distinguish_ownership=true if the story needs ownership, exchange or mistaken identity to be readable. '
    'Before the FIRST appearance of a second same-kind ownership-distinguished object (even across shots), '
    'show an earlier shot with the subject clearly holding their own object with conspicuous distinguishing features visible. '
    'Mark that earlier reference establishes_ownership=true and distinguishing_features_visible=true; otherwise establishes_ownership=false. '
    'Do not put that evidence and the first comparison in the same shot. Each reference owner copies the registered owner. '
)


def planner_schema(schema):
    """Remove redundant writable representations of the same moment."""
    import copy
    schema=copy.deepcopy(schema)
    shot=schema['properties']['shots']['items']['properties']['shot']
    for field in ('description','action','tags'):
        shot['required'].remove(field)
        del shot['properties'][field]
    return schema


def materialize(board):
    """Compile the single structured moment, without rewriting a supplied pose."""
    import copy
    compiled=copy.deepcopy(board)
    for entry in compiled['shots']:
        shot=entry['shot']
        for field in ('action','description','tags'):
            if field in shot:raise ValueError('planner supplied derived field: shot.'+field)
        shot.update(action=shot['pose'],description=shot['pose'],
                    tags=list(dict.fromkeys([shot['pose'],shot['expression']])))
    return compiled
