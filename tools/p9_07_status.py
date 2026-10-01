"""Compact, read-only summary of formal regression jobs."""
import json,sys
from pathlib import Path
from storycg.budget import count_usage
for name in sys.argv[1:]:
    root=Path(name);out={'job':name}
    for filename in ('agent.json','execution.json','cycle.json','plan_log.json'):
        p=root/filename
        if not p.exists():continue
        r=json.loads(p.read_text())
        if filename=='plan_log.json':
            out['planning']=[{'status':x.get('status'),'error':x.get('error'),'rule_id':x.get('rule_id')} for x in r['calls']]
        elif filename=='cycle.json':
            out['cycle']={'phase':r.get('phase'),'blocked':r.get('blocked'),'shots':{n:{'best':s.get('best_attempt'),'rounds':len(s['history']),'stop':s.get('stop'),'diagnosis_error':s.get('diagnosis_error')} for n,s in r.get('shots',{}).items()}}
        else:out[filename]={k:r.get(k) for k in ('phase','reason','error','stage','completed_shots','finished_at') if k in r}
    if (root/'job.json').exists():out['usage']=count_usage(root)
    print(json.dumps(out,ensure_ascii=False))
