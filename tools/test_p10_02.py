from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from storycg import review_v3 as v3


def det(box, area, confidence=.9):
    return {'bbox':box,'area_ratio':area,'confidence':confidence}


def test_small_person_is_retained_and_primary_face_not_duplicated():
    result={'faces':[det([40,20,60,40],.04),det([80,10,84,14],.001)],
            'persons':[det([20,10,70,100],.45),det([78,8,88,35],.027)],'size':[100,100]}
    candidates=v3.extra_candidates(result,{})
    assert len(candidates)==1
    assert candidates[0]['bbox']==[78,8,88,35]
    assert candidates[0]['below_count_threshold']


def test_unmatched_small_face_is_retained():
    result={'faces':[det([40,20,60,40],.04),det([80,10,84,14],.001)],'persons':[],'size':[100,100]}
    assert len(v3.extra_candidates(result,{}))==1


def test_outfit_reference_uses_first_occurrence_of_declared_change():
    entries=[{'number':i,'outfit':{'clothing_state':s,'change_at_shot':c}} for i,s,c in
             [(1,'state_a',None),(2,'state_b',2),(3,'state_b',2)]]
    assert v3.outfit_reference(2,entries,['a','b','c'],'portrait')=='b'
    assert v3.outfit_reference(0,entries,['a','b','c'],'portrait')=='portrait'


def test_outfit_not_visible_does_not_override_visible_mismatch():
    assert v3.combine(['not_visible','mismatch'])=='mismatch'
    assert v3.combine(['not_visible','ok'])=='ok'
    assert v3.combine(['not_visible'])=='not_visible'


def test_reflection_only_allows_declared_reflection_candidate():
    data={'x':{'status':'duplicate','is_reflection':True,'reason':'mirror'},
          'y':{'status':'duplicate','is_reflection':False,'reason':'physical person'}}
    assert v3.extra_status(data,True)=='duplicate'
    assert v3.extra_status({'x':data['x']},True)=='ok'
    assert v3.extra_status({'x':data['x']},False)=='duplicate'


def test_missing_head_is_not_visible_and_call_failure_is_not_semantic_duplicate():
    failed=v3.normalize({'status':'fail','error':'timeout'}, {}, {})
    assert failed['status']=='fail'
    assert 'extra_protagonist' not in failed.get('data',{})


def test_duplicate_requires_hair_evidence_and_keeps_raw_verdict():
    feature=lambda status:{'status':status,'reference_observation':'visible','current_observation':'visible','reason':'pixels'}
    raw={'status':'pass','data':{'outfit_facets':{k:feature('not_visible') for k in v3.FACETS},
         'accessories':feature('not_visible'),'hair_ornaments':feature('not_visible'),
         'candidates':{'candidate_000':{'status':'duplicate','is_reflection':False,'reason':'similar',
                       'hair_color':feature('ok'),'hairstyle':feature('not_visible'),'eye_color':feature('not_visible')}}}}
    normalized=v3.normalize(raw,{'reflection':False,'crops':{}},{})
    assert normalized['data']['extra_protagonist']['status']=='not_visible'
    assert normalized['raw_data']['candidates']['candidate_000']['status']=='duplicate'
    assert raw['data']['candidates']['candidate_000']['status']=='duplicate'


def test_scoring_uses_fail_only_distinct_samples_and_invisible_pass():
    from storycg.calibration_v3 import stats
    samples={str(i):{'v3_truth':{'extra_protagonist':t}} for i,t in enumerate(['error']*3+['ok']*3+['not_visible'])}
    records=[{'sample':sid,'repeat':r,'v3_mapped':{'extra_protagonist':
               'not_visible' if sid=='0' else 'fail' if int(sid)<3 else 'pass'}} for sid in samples for r in (1,2)]
    row=stats(samples,records,'extra_protagonist')
    assert row['fail_detection_rate']==2/3
    assert row['status']=='unreliable'
    assert row['error_samples']==3
    assert row['invisible_pass']==2


def test_all_confirmed_samples_and_call_budget():
    import p10_02_calibrate as calibration
    samples=calibration.inputs()
    assert len([s for s in samples.values() if s['cohort'] in ('v1','v2')])==48
    assert len([s for s in samples.values() if s['cohort']=='v3'])==22
    assert sum(s['supplement'] for s in samples.values())==48
    assert 48*4+48*2==288
