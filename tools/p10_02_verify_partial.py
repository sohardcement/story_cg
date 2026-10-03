"""Read-only verification of a stopped v3 batch; never submits visual requests."""
import copy
import json
from pathlib import Path
from collections import Counter
from jsonschema import Draft202012Validator
from p10_02_calibrate import ROOT,CAL,load,sha,check,prompts
from storycg import review,review_v3
from storycg.calibration_v2 import VISUAL_ITEMS,map_visual,summarize as summarize_old
from storycg.calibration_v3 import NEW_ITEMS,accessory_mapping,stats
from p9_04_execute import comparable_detection


def run():
    frozen=check();result=load(CAL/'results.json')
    assert result['samples']==frozen['samples']
    assert not result['complete']
    assert sha(ROOT/'config/review_reliability.json')==frozen['v2_reliability_sha256']
    events=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
    requests={e['call_id']:e for e in events if e['event']=='request'}
    responses={e['call_id']:e for e in events if e['event']=='response'}
    assert len(requests)==len(responses)==result['usage']['review_calls']<=300
    assert requests.keys()==responses.keys()
    for request in requests.values():
        for message in request['request']['messages']:
            for part in message.get('content',[]):
                if isinstance(part,dict) and part.get('type')=='image_url':
                    image=part['image_url']['url']
                    assert sha(CAL/image['path'])==image['sha256']
    manifest=load(CAL/'manifest.json') if (CAL/'manifest.json').exists() else None
    if manifest:
        assert manifest['samples']==frozen['samples']
        assert manifest['freeze_sha256']==sha(CAL/'reviewer_freeze.json')
        for image in manifest['artifacts']:assert sha(ROOT/image['path'])==image['sha256']
    successful={cid for cid,r in responses.items() if r['status']=='pass'}
    failures={cid:r for cid,r in responses.items() if r['status']!='pass'}
    seen=set();complete=[];partial=[]
    config=load(ROOT/'config/default.json');config['review_reliability_file']=str(CAL/'review_reliability_v2.json')
    for row in result['records']:
        for layer in ('single','consistency','v3'):
            if layer not in row:continue
            record=row[layer];cid=record['call_id'];assert cid not in seen;seen.add(cid)
            response=responses[cid];assert response['status']==record['status']
            if record['status']!='pass':
                assert response['raw_response'] is None and response['error']==record['error']
                assert 'data' not in record
                continue
            raw=json.loads(json.loads(response['raw_response'])['choices'][0]['message']['content'])
            Draft202012Validator(requests[cid]['request']['response_format']['json_schema']['schema']).validate(raw)
            if layer=='v3':
                assert raw==record['raw_data']
                text=requests[cid]['request']['messages'][0]['content'][0]['text']
                prepared=json.loads(text[len(review_v3.PROMPT):]);prepared['crops']=record['crop_evidence']
                expected=review_v3.normalize({'status':'pass','data':raw},prepared,config)
                assert expected['data']==record['data']
                assert expected['validated_candidates']==record['validated_candidates']
                mapped={k:record['data'][k]['status'] for k in NEW_ITEMS}
                mapped.update(accessory_mapping(record,result['samples'][row['sample']]['cohort']))
                assert mapped==row['v3_mapped']
                for crop in record['crop_evidence'].values():
                    if crop:assert sha(crop['path'])==crop['sha256']
            else:
                expected={'status':'pass','data':copy.deepcopy(raw)};review.enforce_visibility(expected)
                if 'framing_observation' in expected['data']:
                    observed=expected['data'].pop('framing_observation')
                    meta=load(ROOT/result['samples'][row['sample']]['metadata'])
                    expected['data']['framing']=review.framing_grade(meta['shot_requirement']['shot']['framing'],observed,config)
                review.annotate_reliability(expected,review.load_reliability(config))
                assert expected['data']==record['data']
        if 'single' in row:
            assert row['mapped']=={k:map_visual(k,row['single'],row['consistency'],cohort=result['samples'][row['sample']]['cohort']) for k in VISUAL_ITEMS}
        ok=all(row.get(k,{}).get('status')=='pass' for k in ('single','consistency'))
        if result['samples'][row['sample']]['supplement']:ok=ok and row.get('v3',{}).get('status')=='pass'
        (complete if ok else partial).append(row)
    assert seen==requests.keys()
    assert all(r['identical'] and comparable_detection(r['first'])==comparable_detection(r['repeat']) for r in result['detector_repeat_checks'])
    counts=Counter(r['sample'] for r in complete);full_ids={sid for sid,count in counts.items() if count==2}
    assert all(count<=2 for count in counts.values())
    samples={sid:v for sid,v in result['samples'].items() if sid in full_ids}
    rows=[r for r in complete if r['sample'] in full_ids]
    summary=summarize_old(samples,rows)
    summary.update({k:stats(samples,rows,k) for k in NEW_ITEMS})
    pending=[{'sample':sid,'repeat':repeat} for sid in result['samples'] for repeat in (1,2)
             if not any(r['sample']==sid and r['repeat']==repeat for r in complete)]
    report={'complete':False,'freeze_valid':True,'original_config_unchanged':True,
            'requests':len(requests),'successful_responses':len(successful),'failures':failures,
            'complete_records':len(complete),'partial_records':len(partial),'complete_samples':sorted(full_ids),
            'pending':pending,'detector_pairs':len(result['detector_repeat_checks']),
            'all_detector_pairs_identical':True,'partial_summary':summary,'usage':result['usage']}
    target=ROOT/'reports/assets/P10-02/partial-verification.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('verified frozen code/inputs/config,',len(successful),'raw successes,',len(failures),'transport failure,',len(complete),'complete records,',len(pending),'pending runs')
    return report


if __name__=='__main__':run()
