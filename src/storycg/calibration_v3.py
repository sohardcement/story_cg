"""v3 fixed scoring; supplements are separate from unchanged regression items."""
from collections import Counter
from storycg.calibration_v2 import summarize as summarize_old

NEW_ITEMS=('extra_protagonist','outfit_detail_vs_portrait','accessories_zoom','hair_ornaments_zoom')


def stats(samples, records, item):
    counts={t:Counter() for t in ('error','ok','not_visible')};sizes=Counter();unstable=pairs=0
    for sid,label in samples.items():
        truth=label.get('v3_truth',{}).get(item)
        if truth not in counts:continue
        rows=[r for r in records if r['sample']==sid]
        if len(rows)!=2:raise ValueError('requires two visual runs')
        statuses=[r['v3_mapped'][item] for r in rows]
        counts[truth].update(statuses);sizes[truth]+=1;pairs+=1;unstable+=statuses[0]!=statuses[1]
    errors=sum(counts['error'].values());oks=sum(counts['ok'].values());nv=sum(counts['not_visible'].values())
    dr=counts['error']['fail']/errors if errors else None;fp=counts['ok']['fail']/oks if oks else None
    return {'status':'unverified' if sizes['error']<3 or sizes['ok']<3 else
            'reliable' if dr>=.75 and fp<=.2 else 'unreliable',
            'error_samples':sizes['error'],'ok_samples':sizes['ok'],'not_visible_samples':sizes['not_visible'],
            'counts':{t:dict(c) for t,c in counts.items()},'fail_detection_rate':dr,'false_positive_rate':fp,
            'invisible_total':nv,'invisible_fail':counts['not_visible']['fail'],'invisible_pass':counts['not_visible']['pass'],
            'not_visible_fail_rate':counts['not_visible']['fail']/nv if nv else None,
            'not_visible_pass_rate':counts['not_visible']['pass']/nv if nv else None,
            'unstable_pairs':unstable,'total_pairs':pairs,'unstable_rate':unstable/pairs if pairs else None,
            'deterministic':False}


def accessory_mapping(result, cohort):
    if result['status']!='pass':return {'accessories_zoom':'fail','hair_ornaments_zoom':'fail'}
    data=result['data'];a=data['accessories_zoom']['status'];h=data['hair_ornaments_zoom']['status']
    # P4 truth explicitly means earrings OR hair ornaments, same fixed mapping as v1.
    if cohort=='v1':
        a='fail' if 'fail' in (a,h) else 'pass' if 'pass' in (a,h) else 'not_visible'
    return {'accessories_zoom':a,'hair_ornaments_zoom':h}


def summarize(result):
    old_samples={k:v for k,v in result['samples'].items() if v['cohort'] in ('v1','v2')}
    old_records=[r for r in result['records'] if r['sample'] in old_samples]
    summary=summarize_old(old_samples,old_records)
    summary.update({item:stats(result['samples'],result['records'],item) for item in NEW_ITEMS})
    for item,row in summary.items():
        nv=row['counts']['not_visible'];row['invisible_pass']=nv.get('pass',0)
        row['not_visible_pass_rate']=nv.get('pass',0)/row['invisible_total'] if row['invisible_total'] else None
    return summary
