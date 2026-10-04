"""Local observations and calibrated grades; preserve raw detection evidence."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image
from storycg import detect

CONFIG_PATH = detect.ROOT / 'config/detection_review.json'


def settings():
    return json.loads(CONFIG_PATH.read_text())


def evidence(status, **values):
    return {'status': status, 'calibration': 'uncalibrated', 'verified': False,
            'reliability_note': '未校准', **values}


def declares_reflection(shot):
    if 'reflection' in shot:
        return shot['reflection'] is True
    # Exact legacy state tag only; prose may negate a reflection.
    return 'reflection' in [str(tag).lower().replace(' ','_') for tag in shot.get('tags',[])]


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def protagonist_count(faces, persons, shot, *, config=None):
    cfg = (config or settings())['count']
    largest = max((f['area_ratio'] for f in faces), default=0)
    face_ids = [i for i,f in enumerate(faces) if f['confidence'] >= cfg['face_confidence']
                and f['area_ratio'] >= largest*cfg['relative_face_area']]
    # Inspect all detector candidates before confidence filtering: a merged box can
    # contain a low-confidence individual, as in the supervisor's calibration.
    merged = [i for i,p in enumerate(persons) if sum(i != j and p['bbox'] != q['bbox']
              and _contains(p['bbox'],q['bbox']) for j,q in enumerate(persons)) >= 2]
    person_ids = [i for i,p in enumerate(persons) if i not in merged
                  and p['confidence'] >= cfg['person_confidence'] and p['area_ratio'] >= cfg['person_area']]
    count = max(len(face_ids),len(person_ids))
    reflection = declares_reflection(shot)
    allowed = 1 + int(reflection)
    # Restricted other_people is recorded, never a blanket exemption for a large
    # extra face/person. Small passersby are already excluded by the fixed rules.
    return evidence('none_visible' if count==0 else 'duplicate' if count>allowed else 'ok',
                    protagonist_count=count, allowed_count=allowed, reflection=reflection,
                    other_people=shot.get('other_people', []), major_face_indices=face_ids,
                    major_person_indices=person_ids, merged_person_indices=merged, thresholds=cfg)


def detect_face_record(source, output, *, faces=None):
    faces = detect.detect_faces(source) if faces is None else faces
    with Image.open(source) as image:
        size = image.size
        if not faces:
            return {'status':'pass','method':'local_face_detector','faces':[],
                    'data':{'found':False,'bbox':[0,0,0,0],'reason':'face_not_detected'}}, None
        selected = max(faces, key=lambda f:(f['area_ratio'],f['confidence']))
        pixels = detect.expand_box(selected['bbox'], size, detect.load_detect_config()['crop']['expansion'])
        bbox = [v/d for v,d in zip(pixels, [size[0],size[1],size[0],size[1]])]
        output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
        image.convert('RGB').crop(pixels).save(output)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    return {'status':'pass','method':'local_face_detector','faces':faces,'selected':selected,
            'data':{'found':True,'bbox':bbox,'reason':'largest detected face; identity unverified'}}, {
                'path':str(output),'sha256':sha,'pixel_bbox':list(pixels),'normalized_bbox':bbox,
                'detector':'local_face_detector'}


def _color_tags(identity, suffix):
    tags = [i['tag'] for i in identity.get('appearance_tags',[]) if isinstance(i,dict) and 'tag' in i]
    tags += identity.get('locked_tags',[])
    tags += [t.strip() for t in identity.get('identity_block','').split(',')]
    return sorted({t.lower().replace(' ','_')[:-len(suffix)] for t in tags if isinstance(t,str)
            and t.lower().replace(' ','_').endswith(suffix) and t.lower().replace(' ','_')[:-len(suffix)] in settings()['colors']['hue_ranges']})


def _hue_distance(a,b):
    return min(abs(a-b),360-abs(a-b))


def _sample(image, face, *, identity=None, config=None):
    cfg = config or settings(); colors = cfg['colors']
    if face is None:
        return evidence('not_visible',reason='face_not_detected',hue=None,saturation=None,pixel_count=0)
    image = detect._image(image)
    x0,y0,x1,y1 = detect.expand_box(face['bbox'],image.size,0)
    roi=detect.expand_box(face['bbox'],image.size,colors['accessory_expansion'])
    selected=np.ones((roi[3]-roi[1],roi[2]-roi[0]),dtype=bool)
    # Accessories are sampled in the ring around the face, not iris/skin.
    selected[y0-roi[1]:y1-roi[1],x0-roi[0]:x1-roi[0]]=False
    hsv=np.array(image.crop(roi).convert('HSV'),dtype=float)
    hue=hsv[:,:,0]*360/255; sat=hsv[:,:,1]/255; val=hsv[:,:,2]/255
    selected &= (sat>=colors['min_saturation']) & (val>=colors['min_value'])
    selected &= ~((hue>=colors['skin_hue'][0]) & (hue<=colors['skin_hue'][1]))
    for hair in _color_tags(identity or {},'_hair'):
        for lo,hi in colors['hue_ranges'].get(hair,[]):selected &= ~((hue>=lo)&(hue<=hi))
    bins=colors['hue_bin_degrees']; hist=np.histogram(hue[selected],bins=np.arange(0,361,bins))[0]
    if not hist.size or not hist.max():
        return evidence('not_visible',reason='no isolated saturated color',hue=None,saturation=None,pixel_count=0,roi=list(roi))
    peak=int(hist.argmax()); center=(peak+.5)*bins
    chosen=selected & (np.minimum(abs(hue-center),360-abs(hue-center))<=colors['cluster_radius'])
    count=int(chosen.sum())
    if count<colors['min_pixels'] or count/max(1,int(selected.sum()))<colors['min_dominant_fraction']:
        return evidence('not_visible',reason='insufficient or ambiguous color pixels',hue=None,saturation=None,pixel_count=count,roi=list(roi))
    angles=np.deg2rad(hue[chosen]); mean=float(np.rad2deg(np.arctan2(np.sin(angles).mean(),np.cos(angles).mean()))%360)
    return evidence('sampled',hue=mean,saturation=float(sat[chosen].mean()),pixel_count=count,
                    candidate_pixels=int(selected.sum()),roi=list(roi))


def sample_accessory_color(image, face, *, reference=None, identity=None, config=None):
    cfg=config or settings()
    if not reference:return evidence('not_visible',reason='no setting reference',hue=None,saturation=None,pixel_count=0)
    ref=_sample(*reference,identity=identity,config=cfg)
    sample=_sample(image,face,identity=identity,config=cfg); sample['reference_sample']=ref
    if ref['status']!='sampled' or sample['status']!='sampled':
        sample['status']='not_visible';sample['reason']='reference/current color not isolated';return sample
    delta=_hue_distance(sample['hue'],ref['hue']);sample['reference_hue_delta']=delta
    # Absence is inconclusive; do not infer a missing accessory from occlusion.
    sample['status']='ok' if delta<=cfg['colors']['reference_hue_tolerance'] else 'not_visible'
    return sample


def audit_detection(source, identity, entry, *, reference_path=None, result=None, config=None, review_config=None):
    cfg=config or settings(); result=detect.detect_image(source) if result is None else result
    face=max(result['faces'],key=lambda f:(f['area_ratio'],f['confidence']),default=None)
    ref=None
    if reference_path:
        ref_faces=detect.detect_faces(reference_path)
        ref_face=max(ref_faces,key=lambda f:(f['area_ratio'],f['confidence']),default=None)
        ref=(reference_path,ref_face)
    observations = {'accessory_color_sampled':sample_accessory_color(source,face,reference=ref,identity=identity,config=cfg),
            'protagonist_count':protagonist_count(result['faces'],result['persons'],entry.get('shot',entry),config=cfg),
            'face_status':'detected' if face else 'face_not_detected',
            'detection':detect.json_result(result)}
    from storycg.review import load_reliability
    normalized = review_detection_items(observations, load_reliability(review_config or {}))
    for key in ACTIVE_DETECTION_KEYS:
        grade = normalized['data'][key]
        observations[key].update({name: grade[name] for name in ('reliability', 'verified', 'reliability_note')})
        observations[key]['grade_status'] = grade['status']
        observations[key]['calibration'] = grade['reliability']
    observations['review'] = normalized
    return observations


def blind_thumbnails(paths, output_dir, *, long_edge=None):
    long_edge=settings()['blind_long_edge'] if long_edge is None else long_edge
    if isinstance(long_edge,bool) or not isinstance(long_edge,int) or long_edge<=0:
        raise ValueError('long edge must be a positive integer')
    output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True)
    outputs=[];records=[]
    for n,path in enumerate(paths,1):
        path=Path(path);output=output_dir/f'{n:02d}.png'
        with Image.open(path) as image:
            image=image.convert('RGB');original=list(image.size)
            image.thumbnail((long_edge,long_edge),Image.Resampling.LANCZOS);image.save(output)
            size=list(image.size)
        outputs.append(output)
        records.append({'source_size':original,'output_size':size,
                        'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
                        'output_path':str(output),'long_edge':long_edge})
    return outputs,records


def repair_mask(source, result, purpose, output_dir, *, config=None):
    """Persist detection-derived mask + provenance; no semantic object guesses."""
    output_dir=Path(output_dir)
    cached=output_dir/'region.json'
    source_sha=hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if cached.exists():
        record=json.loads(cached.read_text())
        if record['provenance']['source_sha256']!=source_sha or record['provenance']['purpose']!=purpose:
            raise ValueError('persisted detection mask source/purpose mismatch')
        path=Path(record['mask_path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['mask_sha256']:
            raise ValueError('persisted detection mask hash mismatch')
        return record
    cfg=config or settings()
    result=detect.detect_image(source) if result is None else result
    size=tuple(result['size']);faces=result['faces'];persons=result['persons']
    count=protagonist_count(faces,persons,{},config=cfg)
    ids=count['major_face_indices']
    primary=max(ids,key=lambda i:faces[i]['area_ratio'],default=None)
    provenance={'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                'purpose':purpose,'primary_face_index':primary,'uncertain':False}
    if purpose=='face':
        if primary is None:raise ValueError('face_not_detected')
        mask=detect.feather_mask(faces[primary],size)
        provenance['method']='detected_face_box'
    else:raise ValueError('unsupported detector mask purpose')
    bounds=mask.getbbox()
    if not bounds:raise ValueError('empty detection mask')
    output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True)
    path=output_dir/'mask.png';mask.save(path)
    record={'mask_path':str(path),'mask_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'bbox':[v/d for v,d in zip(bounds,[size[0],size[1],size[0],size[1]])],
            'provenance':provenance,'size':list(size),'config_snapshot':cfg,'detector_config':detect.load_detect_config()}
    (output_dir/'region.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    return record


# Keep the frozen calibration vocabulary for reading historical observations.
DETECTION_KEYS = ('eye_color_sampled', 'accessory_color_sampled', 'protagonist_count')
ACTIVE_DETECTION_KEYS = ('accessory_color_sampled', 'protagonist_count')


def review_detection_items(observations, reliability):
    """Keep raw detector evidence and normalize its verdicts for shared aggregation."""
    from storycg.review import annotate_reliability
    mapping = {'ok': 'pass', 'mismatch': 'fail', 'duplicate': 'fail',
               'not_visible': 'not_visible', 'none_visible': 'not_visible'}
    data = {}
    # Historical sampled-eye observations remain readable; new audits never produce them.
    keys = (key for key in DETECTION_KEYS if key in observations or key in ACTIVE_DETECTION_KEYS)
    for key in keys:
        observed = observations.get(key, {})
        data[key] = {'status': mapping.get(observed.get('status'), 'fail'),
                     'reason': observed.get('reason', 'local detection: ' + str(observed.get('status'))),
                     'observation': dict(observed)}
    return annotate_reliability({'status': 'pass', 'data': data}, reliability)
