"""Instructions are compiled from exact state references, never free-form nouns."""
from jsonschema import Draft202012Validator

EDIT_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['operation','target','description'], 'properties':{
    'operation':{'enum':['set','remove_surplus','place','remove_duplicate_protagonist']},
    'target':{'type':'string','minLength':1}, 'description':{'type':'string','minLength':1}}}


def state_descriptions(state):
    values={}
    def walk(value,path):
        if isinstance(value,str) and value.strip():values[path]=value
        elif isinstance(value,list):
            for i,v in enumerate(value):walk(v,f'{path}.{i}')
        elif isinstance(value,dict):
            for k,v in value.items():walk(v,f'{path}.{k}')
    for key in ('identity','entry','props'):walk(state[key],key)
    # Targets exclude arbitrary prose, IDs, names and diagnostic hypotheses.
    return {k:v for k,v in values.items() if k in ('identity.identity_block','entry.shot.action') or
            '.description_tags.' in k or '.tags.' in k or (k.startswith(('identity.appearance_tags.','identity.default_outfit_tags.')) and k.endswith('.tag')) or (k.startswith('identity.locked_tags.') and k.endswith('.tag')) or '.position' in k or
            '.relation_tags.' in k or '.location_tags.' in k}


def grounded_instruction(edit,state):
    Draft202012Validator(EDIT_SCHEMA).validate(edit)
    descriptions=state_descriptions(state)
    if descriptions.get(edit['target'])!=edit['description']:
        raise ValueError('instruction must reference an exact existing state description')
    target=edit['target'];description=edit['description']
    if edit['operation']=='remove_duplicate_protagonist':
        if target!='entry.shot.action':raise ValueError('duplicate removal requires shot action')
        background=', '.join(state['entry'].get('scene',{}).get('tags',[]))
        instruction=f'Retain exactly one protagonist, the one whose pose matches this action: {description}. Remove the other protagonist whose pose does not match that action. Fill the vacated area with the surrounding background: {background}.'
    elif edit['operation']=='remove_surplus':
        if not target.startswith('props.'):
            raise ValueError('surplus removal requires a registered prop')
        index=int(target.split('.')[1]);prop=state['props'][index]
        same=[p for p in state['props'] if p['kind']==prop['kind'] and
              any(r['prop_id']==p['id'] for r in state['entry']['shot']['key_props'])]
        descriptions=[]
        for p in same:
            ref=next(r for r in state['entry']['shot']['key_props'] if r['prop_id']==p['id'])
            descriptions.append(', '.join(p['description_tags'])+'; '+ref['position']+'; '+', '.join(ref['relation_tags']+ref['location_tags']))
        if not descriptions:raise ValueError('target object is not active in this shot')
        description=' | '.join(descriptions)
        instruction=f'Remove surplus instances of the registered object kind. Retain exactly {len(same)} registered objects: {description}.'
    elif edit['operation']=='place':
        if not target.startswith('entry.shot.key_props.'):
            raise ValueError('placement requires registered shot prop state')
        ref=state['entry']['shot']['key_props'][int(target.split('.')[3])]
        prop=next(p for p in state['props'] if p['id']==ref['prop_id'])
        instruction=f"Change only the placement of {', '.join(prop['description_tags'])}: {ref['position']}; {', '.join(ref['relation_tags']+ref['location_tags'])}."
    else:
        instruction=f'Correct only this registered visual attribute to exactly: {description}.'
    return instruction+' Preserve every other part, protagonist identity, clothing, composition and drawing style.'
