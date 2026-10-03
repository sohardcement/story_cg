"""One serial v3 calibration on immutable existing images; no generation API."""
import ast
import copy
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from storycg import review, review_v3, detect, detection_review
from storycg.calibration_v2 import VISUAL_ITEMS, map_visual, summarize as summarize_old
from storycg.calibration_v3 import NEW_ITEMS, accessory_mapping, summarize
from storycg.state import save_json_atomic
from p9_04_calibrate import inputs as old_inputs
from p9_04_continue import normalize_entry
from p9_04_execute import comparable_detection

ROOT=Path(__file__).resolve().parents[1];CAL=ROOT/'calibration/v3'
LABELS=['verify/P4-labels.json','verify/P9-labels.json','verify/P10-labels.json']
FILES=sorted([str(p.relative_to(ROOT)) for p in (ROOT/'src/storycg').glob('*.py')])+[
 'tools/p10_02_calibrate.py','tools/test_p10_02.py','tests/test_review.py','tests/test_p3_02.py','tools/p9_04_calibrate.py',
 'tools/p9_04_continue.py','tools/p9_04_execute.py','tools/p9_04_resume.py',
 'config/default.json','config/detect.json','config/detection_review.json','config/identity_rules.json',
 'models/detect/face_yolov8s.pt','models/detect/person_yolov8s-seg.pt']


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):return json.loads(Path(path).read_text())


def materialize(path, old):
    path=Path(path);digest=sha(path)
    matching=[v for v in old.values() if v['image_sha256']==digest]
    if matching:
        label=matching[0];meta=load(ROOT/label['metadata'])
        return meta['identity'],meta['shot_requirement'],meta['props'],ROOT/label['reference'],[ROOT/label['metadata']]
    for parent in path.parents:
        if not parent.is_relative_to(ROOT):break
        if (parent/'identity.json').is_file() and (parent/'storyboard.json').is_file():
            board=load(parent/'storyboard.json');manifest=load(parent/'manifest.json')
            matched=[int(i['shot_id'].removeprefix('shot')) for i in manifest['images'] if (parent/i['path']).resolve()==path.resolve()]
            shot_match=re.search(r'_shot(\d+)_a\d+\.png$',path.name)
            if not matched and not shot_match:raise ValueError('cannot resolve shot from manifest or generated artifact name')
            number=matched[0] if matched else int(shot_match.group(1))
            ref=parent/load(parent/'portrait.json')['image']['path'] if (parent/'portrait.json').is_file() else parent/manifest['images'][0]['path']
            return load(parent/'identity.json'),board['shots'][number-1],board.get('props',[]),ref,[parent/n for n in ('identity.json','storyboard.json','manifest.json')]
    raise ValueError('no state-owned identity/shot for '+str(path))


def inputs():
    old=old_inputs();samples=copy.deepcopy(old)
    for sid,label in samples.items():
        label['v3_truth']={}
        if 'accessories' in label['truth']:label['v3_truth']['accessories_zoom']=label['truth']['accessories']
        if 'hair_ornaments' in label['truth']:label['v3_truth']['hair_ornaments_zoom']=label['truth']['hair_ornaments']
        label['supplement']=bool(label['v3_truth'])
        label['input_hashes']={label['metadata']:sha(ROOT/label['metadata'])}
    for sid,label in load(ROOT/LABELS[2])['samples'].items():
        if not isinstance(label,dict):continue
        image=ROOT/label['path'];identity,entry,props,reference,state_paths=materialize(image,old)
        if label.get('portrait'):reference=ROOT/label['portrait']
        entry=copy.deepcopy(entry)
        if 'reflection' in label:entry['shot']['reflection']=label['reflection']
        key='v3:'+sid
        samples[key]={'cohort':'v3','sample_id':sid,'truth':{},'v3_truth':label['truth'],'supplement':True,
            'image':str(image.relative_to(ROOT)),'image_sha256':sha(image),'reference':str(reference.relative_to(ROOT)),
            'reference_sha256':sha(reference),'state':{'identity':identity,'shot_requirement':entry,'props':props},
            'input_hashes':{str(p.relative_to(ROOT)):sha(p) for p in state_paths}}
    return samples


def prompts():
    return {p:[n.value for n in ast.walk(ast.parse((ROOT/p).read_text())) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
            for p in FILES if p.endswith('.py')}


def freeze():
    CAL.mkdir(parents=True,exist_ok=True)
    if (CAL/'reviewer_freeze.json').exists():raise ValueError('freeze exists')
    samples=inputs();calls=sum(4 if v['cohort']!='v3' else 0 for v in samples.values())+sum(2 for v in samples.values() if v['supplement'])
    if calls>300:raise ValueError('planned budget exceeded')
    shutil.copyfile(ROOT/'config/review_reliability.json',CAL/'review_reliability_v2.json')
    frozen={'at':datetime.now(timezone.utc).isoformat(),'files':{p:sha(ROOT/p) for p in FILES},
        'prompt_texts':prompts(),'detection_thresholds':load(ROOT/'config/detection_review.json'),
        'v3_crop_settings':review_v3.SETTINGS,'samples':samples,'labels':{p:sha(ROOT/p) for p in LABELS},
        'v2_reliability_sha256':sha(CAL/'review_reliability_v2.json'),'v2_results_sha256':sha(ROOT/'calibration/v2/results.json'),
        'rules':{'minimum_error_samples':3,'minimum_ok_samples':3,'fail_detection_min':.75,'false_positive_max':.2,
                 'visual_repeats':2,'detector_scored_repeats':1},'expected_visual_calls':calls}
    save_json_atomic(CAL/'reviewer_freeze.json',frozen);print('frozen',len(samples),'samples',calls,'calls')


def check():
    f=load(CAL/'reviewer_freeze.json')
    assert f['files']=={p:sha(ROOT/p) for p in FILES},'reviewer/executor changed'
    assert f['prompt_texts']==prompts(),'prompts changed'
    assert f['samples']==inputs(),'inputs changed'
    assert f['labels']=={p:sha(ROOT/p) for p in LABELS},'labels changed'
    assert f['v2_reliability_sha256']==sha(CAL/'review_reliability_v2.json')
    assert f['v2_results_sha256']==sha(ROOT/'calibration/v2/results.json')
    return f


def run():
    f=check();output=CAL/'results.json'
    if output.exists() or (CAL/'started.json').exists():raise ValueError('batch already started; no rerun')
    started=time.monotonic();save_json_atomic(CAL/'started.json',{'at':datetime.now(timezone.utc).isoformat()})
    config=load(ROOT/'config/default.json');config['review_reliability_file']=str(CAL/'review_reliability_v2.json')
    vlm=review.VisionClient(CAL,config['model_service_url'],max_calls=300,max_seconds=7800)
    result={'version':3,'complete':False,'samples':f['samples'],'records':[],'detector_repeat_checks':[],'freeze_sha256':sha(CAL/'reviewer_freeze.json')}
    def persist():
        result['usage']={'generated_images':0,'review_calls':vlm.calls,'vision_seconds':vlm.call_seconds,
                         'elapsed_seconds':time.monotonic()-started,'detector_scored_runs':len(result['detector_repeat_checks'])}
        save_json_atomic(output,result)
    try:
        for sid,label in f['samples'].items():
            sample=load(ROOT/label['metadata']) if label.get('metadata') else label['state']
            entry,adapter=normalize_entry(sample['shot_requirement']);identity=sample['identity']
            dest=CAL/'artifacts'/sid.replace(':','_');dest.mkdir(parents=True,exist_ok=True)
            target,reference=dest/'current.png',dest/'reference.png'
            shutil.copyfile(ROOT/label['image'],target);shutil.copyfile(ROOT/label['reference'],reference)
            observations=detection_review.audit_detection(target,identity,entry,reference_path=reference,review_config=config)
            repeated=detection_review.audit_detection(target,identity,entry,reference_path=reference,review_config=config)
            same=comparable_detection(observations)==comparable_detection(repeated)
            result['detector_repeat_checks'].append({'sample':sid,'identical':same,'first':observations,'repeat':repeated,'scored_runs':1,
                                                     'ignored_metadata':['detection.timing_seconds']})
            if not same:raise ValueError('detector non-deterministic')
            paths=[reference,target]
            _,face=detection_review.detect_face_record(target,dest/'current_face.png')
            _,ref_face=detection_review.detect_face_record(reference,dest/'reference_face.png')
            if face and ref_face:
                pair=dest/'face_pair.png';review.face_pair(dest/'reference_face.png',dest/'current_face.png',pair);paths.append(pair)
            prepared=review_v3.prepare(target,reference,identity,entry,dest/'v3') if label['supplement'] else None
            for repeat in (1,2):
                purpose=sid.replace(':','_')+f'_run{repeat}'
                record={'sample':sid,'repeat':repeat,'detection_review':observations['review'],'v3_mapped':{},'input_adapter':adapter}
                if label['cohort']!='v3':
                    single=review.audit_single(vlm,target,entry,sample['props'],config,purpose+'_single')
                    consistency=review.audit_consistency(vlm,paths,review.identity_checklist(identity,entry['outfit']),config,purpose+'_consistency')
                    record.update(single=single,consistency=consistency,mapped={item:map_visual(item,single,consistency,cohort=label['cohort']) for item in VISUAL_ITEMS})
                    if single['status']!='pass' or consistency['status']!='pass':
                        result['records'].append(record);persist();raise ValueError('visual transport/schema failure; no image grade substituted')
                if prepared:
                    supplementary=review_v3.audit(vlm,prepared,config,purpose+'_v3');record['v3']=supplementary
                    if supplementary['status']!='pass':
                        result['records'].append(record);persist();raise ValueError('v3 transport/schema failure; no image grade substituted')
                    record['v3_mapped']={item:supplementary['data'][item]['status'] for item in NEW_ITEMS}
                    record['v3_mapped'].update(accessory_mapping(supplementary,label['cohort']))
                result['records'].append(record);persist()
                print(sid,repeat,'calls',vlm.calls,'v3',record['v3_mapped'],flush=True)
        check();result['summary']=summarize(result)
        old_samples={k:v for k,v in result['samples'].items() if v['cohort'] in ('v1','v2')}
        old_records=[r for r in result['records'] if r['sample'] in old_samples]
        result['cohort_summary']={c:summarize_old(old_samples,old_records,cohort=c) for c in ('v1','v2')}
        result['v2_baseline']=load(ROOT/'calibration/v2/results.json')['summary']
        result['complete']=True;persist();publish(result);verify()
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}';persist();raise
    finally:vlm.close()


def publish(result):
    old=load(CAL/'review_reliability_v2.json');items=copy.deepcopy(result['summary'])
    zoom=items['accessories_zoom'];rate=zoom['fail_detection_rate']
    if rate is None or rate<.75:
        zoom['status']='unreliable';zoom['decision']='D22 转 (a)：配饰继续标注未经可靠审核'
    # Original accessory methods keep their own measured reliability; zoom does not upgrade them.
    save_json_atomic(ROOT/'config/review_reliability.json',{'version':3,'items':items,'rule':old['rule'],
        'freeze':load(CAL/'reviewer_freeze.json')['files'],'calibration_results':'calibration/v3/results.json'})
    pct=lambda x:'—' if x is None else f'{x:.1%}'
    lines=['# P10-02 审核器 v3 校准','',
        '既有图片、固定真值。视觉每样本两次；检测第一次计分、第二次仅核对确定性。检出只计 fail。',
        'error/ok 各 ≥3，检出 ≥75%，误报 ≤20%。原项与新增项分别评分，不把旧回归改判成新增结论。','',
        '| 项 | v2 检出/误报 | v3 检出/误报 | error/ok 样本 | not_visible→pass/fail | 两次不一致 | 可靠性 |',
        '|---|---|---|---|---|---|---|']
    for key,row in result['summary'].items():
        baseline=result['v2_baseline'].get(key,{})
        lines.append(f"| {key} | {pct(baseline.get('fail_detection_rate'))}/{pct(baseline.get('false_positive_rate'))} | {pct(row['fail_detection_rate'])}/{pct(row['false_positive_rate'])} | {row['error_samples']}/{row['ok_samples']} | {row['invisible_pass']}/{row['invisible_fail']} (分母 {row['invisible_total']}) | {row['unstable_pairs']}/{row['total_pairs']} | {items[key]['status']} |")
    lines+=['',zoom.get('decision','D22 放大项达到检出门槛；仍须同时满足误报和样本数门槛，D20 层级规则保持。'),
        '', 'P4 配饰按真值定义合并耳饰/发饰；P9 配饰、发饰单列。两个 cohort 重复图片作为原确认条目保留。',
        '新单主角项独立校准；原本机计数仍独立评分，不假借旧计数 reliable 升格新项。',
        '服装细节只按可见分项判断，一项 mismatch 即 fail；其余看不见不抵消可见错误。',
        '原图、真值、v2 results/config 副本未修改；裁切是已有像素放大，不是新生成图片。','',
        '用量：'+json.dumps(result['usage'],ensure_ascii=False),'', '## 新增项逐样本两次判定','',
        '| 样本 | 真值 | 第一次 | 第二次 |','|---|---|---|---|']
    for sid,label in result['samples'].items():
        if not label['v3_truth']:continue
        rows=[r for r in result['records'] if r['sample']==sid]
        lines.append(f"| {sid} | {json.dumps(label['v3_truth'],ensure_ascii=False)} | {json.dumps(rows[0]['v3_mapped'])} | {json.dumps(rows[1]['v3_mapped'])} |")
    (CAL/'summary.md').write_text('\n'.join(lines)+'\n')


def verify():
    f=check();result=load(CAL/'results.json');assert result['complete']
    assert result['samples']==f['samples'];assert len(result['records'])==len(f['samples'])*2
    assert all(r['identical'] and comparable_detection(r['first'])==comparable_detection(r['repeat']) for r in result['detector_repeat_checks'])
    events=[json.loads(line) for line in (CAL/'review_log.jsonl').read_text().splitlines()]
    requests={e['call_id']:e for e in events if e['event']=='request'};responses={e['call_id']:e for e in events if e['event']=='response'}
    assert len(requests)==f['expected_visual_calls']==result['usage']['review_calls']
    assert requests.keys()==responses.keys()
    from jsonschema import Draft202012Validator
    seen=set()
    for row in result['records']:
        for key in ('single','consistency','v3'):
            if key not in row:continue
            r=row[key];cid=r['call_id'];assert cid not in seen;seen.add(cid)
            raw=json.loads(json.loads(responses[cid]['raw_response'])['choices'][0]['message']['content'])
            Draft202012Validator(requests[cid]['request']['response_format']['json_schema']['schema']).validate(raw)
            if key=='v3':
                assert raw==r['raw_data']
                text=requests[cid]['request']['messages'][0]['content'][0]['text']
                context=json.loads(text[len(review_v3.PROMPT):]);context['crops']=r['crop_evidence']
                normalized=review_v3.normalize({'status':'pass','data':raw},context,{})
                for item,value in normalized['data'].items():
                    for field in ('status','observed_status','reason'):assert r['data'][item][field]==value[field]
                mapped={item:r['data'][item]['status'] for item in NEW_ITEMS}
                mapped.update(accessory_mapping(r,result['samples'][row['sample']]['cohort']))
                assert mapped==row['v3_mapped']
                for crop in r['crop_evidence'].values():
                    if crop:assert sha(crop['path'])==crop['sha256']
            else:
                normalized={'status':'pass','data':copy.deepcopy(raw)};review.enforce_visibility(normalized)
                if 'framing_observation' in normalized['data']:
                    observed=normalized['data'].pop('framing_observation')
                    label=result['samples'][row['sample']];meta=load(ROOT/label['metadata'])
                    normalized['data']['framing']=review.framing_grade(meta['shot_requirement']['shot']['framing'],observed,load(ROOT/'config/default.json'))
                for item,value in normalized['data'].items():
                    assert all(r['data'][item][k]==v for k,v in value.items())
        if 'single' in row:
            assert row['mapped']=={item:map_visual(item,row['single'],row['consistency'],cohort=result['samples'][row['sample']]['cohort']) for item in VISUAL_ITEMS}
    assert seen==requests.keys();assert result['summary']==summarize(result)
    config=load(ROOT/'config/review_reliability.json');assert config['version']==3
    for key,row in result['summary'].items():
        expected=copy.deepcopy(row)
        if key=='accessories_zoom' and (row['fail_detection_rate'] is None or row['fail_detection_rate']<.75):
            expected['status']='unreliable';expected['decision']='D22 转 (a)：配饰继续标注未经可靠审核'
        assert config['items'][key]==expected
    print('verified freeze, all raw responses, mappings, statistics, config; calls',len(requests))


if __name__=='__main__':{'freeze':freeze,'check':check,'run':run,'verify':verify}[sys.argv[1]]()
