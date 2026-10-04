"""Detection-grounded supplementary audits against state-owned references.

No image generation; crops enlarge existing pixels, never reconstruct details.
"""
import copy
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps
from storycg import detect, detection_review, review

SETTINGS = {'head_expansion': .8, 'head_long_edge': 768,
            'person_expansion': .15, 'candidate_long_edge': 768,
            'person_head_fraction': .4}
FACETS = ('sleeve_length', 'neckline', 'upper_length', 'skirt_or_trouser_length',
          'outer_layers', 'major_color_blocks')
ALL_ITEMS = ('extra_protagonist', 'outfit_detail_vs_portrait', 'accessories_zoom', 'hair_ornaments_zoom')
PROMPT = (
    'Audit only the supplied reference/current pixels and script-owned state. '
    'Images are labeled by the image index map. Enlargements contain no new detail. '
    'For each clothing facet describe reference and current observations separately; '
    'compare sleeve length (including sleeveless), neckline, upper garment length, '
    'skirt/trouser hem relative to body, outer garment presence, and major color blocks. '
    'Use the selected outfit reference, NOT a different outfit of the same character. '
    'ok=visible match; mismatch=concrete visible difference; not_visible=angle, occlusion, '
    'out_of_frame or too_small prevents comparison. One visible mismatch makes the outfit mismatch; '
    'do not infer concealed lengths or sleeves. State tags are not evidence that a visible detail matches. '
    'For head accessories and hair ornaments compare each expected item in the expanded head images '
    'and reference/current full images. Inspect exposed ear/head regions before declaring absence. '
    'If the expected region is exposed but the item is absent, mismatch; if the region cannot be '
    'inspected, not_visible. Never pass from reference or expectation alone. '
    'For EVERY extra candidate compare actual hair color and hairstyle with locked identity and '
    'reference head, plus eye color when visible. Report those three feature comparisons and '
    'concrete observations. duplicate means another depiction of the protagonist, supported by '
    'matching hair color AND hairstyle; a visible conflicting eye color is contrary evidence. '
    'A stranger or isolated hand is ok; insufficient identity detail is not_visible. '
    'Evaluate large and small candidates identically. Explicitly identify actual reflections; '
    'only a storyboard-declared reflection permits a reflected copy, never an extra physical person. '
    'Do not use character fame, scene expectations or clothing alone to infer identity. '
)


def combine(statuses):
    statuses=list(statuses)
    return 'mismatch' if 'mismatch' in statuses else 'ok' if 'ok' in statuses else 'not_visible'


def extra_status(data, reflection):
    statuses=[('ok' if reflection and item.get('is_reflection') else item['status'])
              for item in data.values()]
    return 'duplicate' if 'duplicate' in statuses else 'not_visible' if 'not_visible' in statuses else 'ok'


def _iou(a, b):
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union>0 else 0


def extra_candidates(result, shot):
    faces,persons=result['faces'],result['persons']
    counts=detection_review.protagonist_count(faces,persons,shot)
    merged=set(counts['merged_person_indices'])
    primary_face=max(range(len(faces)), key=lambda i:(faces[i]['area_ratio'],faces[i]['confidence']), default=None)
    eligible=[i for i in range(len(persons)) if i not in merged]
    containing=[i for i in eligible if primary_face is not None and
                detection_review._contains(persons[i]['bbox'],faces[primary_face]['bbox'])]
    primary_person=(min(containing,key=lambda i:persons[i]['area_ratio']) if containing else
                    max(eligible,key=lambda i:persons[i]['area_ratio'],default=None))
    candidates=[]
    for i in eligible:
        if i==primary_person or i in containing:continue
        if primary_person is not None and _iou(persons[i]['bbox'],persons[primary_person]['bbox'])>=.5:continue
        candidates.append({'kind':'person','index':i,**{k:persons[i][k] for k in ('bbox','confidence','area_ratio')},
                           'below_count_threshold':i not in counts['major_person_indices']})
    for i,face in enumerate(faces):
        if i==primary_face:continue
        if any(detection_review._contains(p['bbox'],face['bbox']) for p in candidates):continue
        candidates.append({'kind':'face','index':i,**{k:face[k] for k in ('bbox','confidence','area_ratio')},
                           'below_count_threshold':i not in counts['major_face_indices']})
    return candidates


def outfit_reference(index, entries, paths, portrait, outfit_portraits=None):
    entry=entries[index];outfit=entry['outfit'];change=outfit.get('change_at_shot')
    if change is None:return portrait or paths[0]
    signature=outfit['clothing_state']
    if outfit_portraits and signature in outfit_portraits:return outfit_portraits[signature]
    first=next((i for i,e in enumerate(entries[:index+1]) if
                e['number']==change and e['outfit']['clothing_state']==signature),None)
    if first is None:raise ValueError('declared outfit change has no matching first occurrence')
    return paths[first]


def crop(source, bbox, output, expansion, long_edge):
    with Image.open(source) as image:
        pixels=detect.expand_box(bbox,image.size,expansion)
        region=image.convert('RGB').crop(pixels)
        scale=long_edge/max(region.size)
        region=region.resize((max(1,round(region.width*scale)),max(1,round(region.height*scale))),Image.Resampling.LANCZOS)
        Path(output).parent.mkdir(parents=True,exist_ok=True);region.save(output)
    return {'path':str(output),'pixel_bbox':list(pixels),'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'sha256':hashlib.sha256(Path(output).read_bytes()).hexdigest(),'expansion':expansion}


def head(source, result, output):
    face=max(result['faces'],key=lambda f:f['area_ratio'],default=None)
    if face:return crop(source,face['bbox'],output,SETTINGS['head_expansion'],SETTINGS['head_long_edge'])
    persons=result['persons']; merged=detection_review.protagonist_count([],persons,{})['merged_person_indices']
    person=max((p for i,p in enumerate(persons) if i not in merged),key=lambda p:p['area_ratio'],default=None)
    if not person:return None
    x0,y0,x1,y1=person['bbox']
    record=crop(source,[x0,y0,x1,y0+(y1-y0)*SETTINGS['person_head_fraction']],output,
                SETTINGS['person_expansion'],SETTINGS['head_long_edge'])
    record['method']='upper_person_box_without_face';return record


def prepare(current, reference, identity, entry, directory, *, result=None, reference_result=None, items=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    items=ALL_ITEMS if items is None else tuple(items)
    needs_head=any(key in items for key in ('extra_protagonist','accessories_zoom','hair_ornaments_zoom'))
    result=detect.detect_image(current) if result is None and needs_head else result
    reference_result=detect.detect_image(reference) if reference_result is None and needs_head else reference_result
    paths=[Path(reference),Path(current)];index_map={'reference_full':0,'current_full':1}
    crops={}
    for label,source,detection in ([('reference_head',reference,reference_result),('current_head',current,result)] if needs_head else []):
        record=head(source,detection,directory/(label+'.png'))
        crops[label]=record
        if record:index_map[label]=len(paths);paths.append(Path(record['path']))
    candidates=extra_candidates(result,entry.get('shot',entry)) if 'extra_protagonist' in items else []
    for i,candidate in enumerate(candidates):
        key=f'candidate_{i:03d}'
        record=crop(current,candidate['bbox'],directory/(key+'.png'),
                    SETTINGS['head_expansion'] if candidate['kind']=='face' else SETTINGS['person_expansion'],
                    SETTINGS['candidate_long_edge'])
        crops[key]=record;index_map[key]=len(paths);paths.append(Path(record['path']))
        candidate['key']=key
    return {'paths':paths,'index_map':index_map,'crops':crops,'candidates':candidates,'items':items,
            'reflection':detection_review.declares_reflection(entry.get('shot',entry)),
            'checks':review.identity_checklist(identity,entry['outfit']),
            'locked_tags':identity.get('locked_tags',[]),'outfit':entry['outfit']}


def grade_schema(statuses):
    return review.object_schema({'status':{'enum':statuses},'reference_observation':review.TEXT,
                                'current_observation':review.TEXT,'reason':review.TEXT})


def schema(prepared):
    grade=grade_schema(['ok','mismatch','not_visible'])
    candidate=review.object_schema({'status':{'enum':['ok','duplicate','not_visible']},
              'is_reflection':{'type':'boolean'},'reason':review.TEXT,
              **{key:grade for key in ('hair_color','hairstyle','eye_color')}})
    properties={'outfit_facets':review.object_schema({key:grade for key in FACETS}),
          'accessories':grade,'hair_ornaments':grade,
          'candidates':review.object_schema({c['key']:candidate for c in prepared['candidates']})}
    fields={'outfit_facets':'outfit_detail_vs_portrait','accessories':'accessories_zoom',
            'hair_ornaments':'hair_ornaments_zoom','candidates':'extra_protagonist'}
    return review.object_schema({key:value for key,value in properties.items()
        if fields[key] in prepared.get('items',ALL_ITEMS)})


def normalize(raw, prepared, config):
    result=copy.deepcopy(raw)
    if raw['status']!='pass':return result
    data=raw['data'];reflection=prepared['reflection'];candidates=data.get('candidates',{})
    # Derive identity evidence without using the model's candidate-level verdict.
    candidates=copy.deepcopy(candidates)
    for item in candidates.values():
        hair=item['hair_color']['status'];hairstyle=item['hairstyle']['status'];eyes=item['eye_color']['status']
        if hair=='ok' and hairstyle=='ok' and eyes!='mismatch' and not (reflection and item['is_reflection']):
            item['status']='duplicate'
        elif 'mismatch' not in (hair,hairstyle,eyes) and 'not_visible' in (hair,hairstyle):
            item['status']='not_visible'
        else:item['status']='ok'
    mapping={'ok':'pass','duplicate':'fail','mismatch':'fail','not_visible':'not_visible'}
    verdicts={}
    if 'candidates' in data:verdicts['extra_protagonist']=extra_status(candidates,reflection)
    if 'outfit_facets' in data:verdicts['outfit_detail_vs_portrait']=combine(i['status'] for i in data['outfit_facets'].values())
    if 'accessories' in data:verdicts['accessories_zoom']=data['accessories']['status']
    if 'hair_ornaments' in data:verdicts['hair_ornaments_zoom']=data['hair_ornaments']['status']
    result['raw_data']=copy.deepcopy(data)
    result['validated_candidates']=candidates
    result['data']={key:{'status':mapping[value],'observed_status':value,
                         'reason':json.dumps(candidates if key=='extra_protagonist' else
                          data['outfit_facets'] if key=='outfit_detail_vs_portrait' else
                          data['accessories' if key=='accessories_zoom' else 'hair_ornaments'],ensure_ascii=False)}
                    for key,value in verdicts.items()}
    # Cropping failure cannot establish an accessory match, even if the full image suggests it.
    if not all(prepared['crops'].get(k) for k in ('reference_head','current_head')):
        for key in ('accessories_zoom','hair_ornaments_zoom'):
            if key in result['data']:
                result['data'][key].update(status='not_visible',observed_status='not_visible',reason='head region not detected in reference/current')
    return review.annotate_reliability(result,review.load_reliability(config))


def audit(vlm, prepared, config, purpose):
    context={key:prepared[key] for key in ('index_map','candidates','reflection','checks','locked_tags','outfit')}
    # Keep the full original prompt when all research items are explicitly requested.
    items=prepared.get('items',ALL_ITEMS)
    prompt=PROMPT
    if set(items)!=set(ALL_ITEMS):
        outfit_start=PROMPT.index('For each clothing facet')
        head_start=PROMPT.index('For head accessories')
        count_start=PROMPT.index('For EVERY extra candidate')
        prompt=PROMPT[:outfit_start]
        if 'outfit_detail_vs_portrait' in items:prompt+=PROMPT[outfit_start:head_start]
        if 'accessories_zoom' in items or 'hair_ornaments_zoom' in items:prompt+=PROMPT[head_start:count_start]
        if 'extra_protagonist' in items:prompt+=PROMPT[count_start:]
    raw=vlm.call(purpose,review.messages(prompt+json.dumps(context,ensure_ascii=False),prepared['paths']),
                 schema(prepared),prepared['paths'])
    result=normalize(raw,prepared,config)
    result['crop_evidence']=prepared['crops'];result['image_index_map']=prepared['index_map']
    return result


def enabled_items(config):
    switches=config.get('supplementary_review',{})
    return tuple(key for key in ALL_ITEMS if switches.get(
        'accessories_zoom' if key=='hair_ornaments_zoom' else key,False) is True)


def enabled(config):
    return bool(enabled_items(config))
