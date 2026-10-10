"""Instructions are compiled from exact state references, never free-form nouns."""
from jsonschema import Draft202012Validator

EDIT_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['operation','target','description'], 'properties':{
    'operation':{'enum':['set','remove_duplicate_protagonist']},
    'target':{'type':'string','minLength':1}, 'description':{'type':'string','minLength':1}}}


def state_descriptions(state):
    values={}
    def walk(value,path):
        if isinstance(value,str) and value.strip():values[path]=value
        elif isinstance(value,list):
            for i,v in enumerate(value):walk(v,f'{path}.{i}')
        elif isinstance(value,dict):
            for k,v in value.items():walk(v,f'{path}.{k}')
    for key in ('identity','entry'):walk(state[key],key)
    # Targets exclude arbitrary prose, IDs, names and diagnostic hypotheses.
    return {k:v for k,v in values.items() if k in ('identity.identity_block','entry.shot.action') or
            '.tags.' in k or (k.startswith(('identity.appearance_tags.','identity.default_outfit_tags.')) and k.endswith('.tag')) or (k.startswith('identity.locked_tags.') and k.endswith('.tag'))}


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
    else:
        instruction=f'Correct only this registered visual attribute to exactly: {description}.'
    return instruction+' Preserve every other part, protagonist identity, clothing, composition and drawing style.'
