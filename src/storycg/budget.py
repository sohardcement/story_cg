"""Job-wide counts include tier/portrait tasks and are checked at service boundaries."""
import json
from datetime import datetime, timezone
from pathlib import Path
from storycg import review


class BudgetExceeded(ValueError):pass


def read(path):return json.loads(Path(path).read_text())


def count_usage(root):
    root=Path(root);generated=posts=vision=llm=0
    for path in root.rglob('state.json'):
        for a in read(path).get('attempts',[]):
            if not a.get('inherited'):
                generated+=a['status']=='completed';posts+=a.get('post_count',0)
    for path in root.rglob('review_log.jsonl'):
        for line in path.read_text().splitlines():
            r=json.loads(line)
            if r.get('event')=='request':
                if r.get('purpose','').startswith('cycle_llm_'):llm+=1
                else:vision+=1
    for path in root.rglob('plan_log.json'):
        llm+=len(read(path)['calls'])
    for path in root.rglob('agent_llm_log.json'):
        llm+=len(read(path)['calls'])
    job=read(root/'job.json')
    ended=None
    for name in ('execution.json','cycle.json','agent.json'):
        path=root/name
        if path.exists():
            record=read(path)
            if record.get('finished_at'):ended=datetime.fromisoformat(record['finished_at'])
    elapsed=((ended or datetime.now(timezone.utc))-datetime.fromisoformat(job['created_at'])).total_seconds()
    execution_path=root/'execution.json'
    paused=json.loads(execution_path.read_text()).get('supervisor_wait_seconds',0) if execution_path.exists() else 0
    agent_path=root/'agent.json'
    if agent_path.exists():
        agent=read(agent_path)
        paused+=agent.get('paused_seconds',0)
        if agent.get('waiting_since'):
            paused+=max(0,((ended or datetime.now(timezone.utc))-datetime.fromisoformat(agent['waiting_since'])).total_seconds())
    wall_elapsed=elapsed
    elapsed=max(0,elapsed-paused)
    return {'generated_images':generated,'submission_attempts':posts,'review_calls':vision,
            'llm_calls':llm,'elapsed_seconds':round(elapsed,2), 'wall_elapsed_seconds':round(wall_elapsed,2)}


class Budget:
    def __init__(self,root,settings):self.root=Path(root);self.settings=settings
    def usage(self):return count_usage(self.root)
    def remaining_seconds(self):return max(0,self.settings['max_minutes']*60-self.usage()['elapsed_seconds'])
    def check(self,*,images=0,vision=0,llm=0):
        u=self.usage();s=self.settings
        if u['submission_attempts']+images>s['max_images']:raise BudgetExceeded('image_budget')
        if u['review_calls']+vision>s['max_vision_calls']:raise BudgetExceeded('vision_budget')
        if u['llm_calls']+llm>s['max_llm_calls']:raise BudgetExceeded('llm_budget')
        if u['elapsed_seconds']>=s['max_minutes']*60:raise BudgetExceeded('time_budget')


class CycleClient(review.VisionClient):
    def __init__(self,root,url,*,budget,**kwargs):
        self.budget=budget
        super().__init__(root,url,**kwargs)
    def call(self,purpose,*args,**kwargs):
        try:self.budget.check(llm=1) if purpose.startswith('cycle_llm_') else self.budget.check(vision=1)
        except BudgetExceeded as exc:return {'status':'fail','error':str(exc),'purpose':purpose}
        self.max_seconds=min(self.max_seconds,(__import__('time').monotonic()-self.started)+self.budget.remaining_seconds())
        return super().call(purpose,*args,**kwargs)


class GuardedComfy:
    def __init__(self,client,budget):self.client=client;self.budget=budget
    def __getattr__(self,name):return getattr(self.client,name)
    def submit_prompt(self,workflow,client_id):
        # Runner records post_count before this call; check the already reserved total.
        self.budget.check()
        return self.client.submit_prompt(workflow,client_id)
    def wait_for_history(self,prompt_id,interval_seconds,timeout_seconds):
        return self.client.wait_for_history(prompt_id,interval_seconds,min(timeout_seconds,self.budget.remaining_seconds()))
