"""Current-order data driven candidate search; does not submit images."""
import json,time
from pathlib import Path
from datetime import datetime
import httpx
from storycg.state import save_json_atomic
from storycg.identity import USER_AGENT
root=Path('reports/assets/P6-02');start=time.monotonic()
release=json.loads((root/'release.json').read_text())['body']['publishedAt']
tags=json.loads((root/'recent-tags.json').read_text())['response']
with httpx.Client(base_url='https://danbooru.donmai.us',timeout=30,trust_env=False,headers={'User-Agent':USER_AGENT}) as c:
 rows=[]
 for tag in tags:
  if datetime.fromisoformat(tag['created_at'])<=datetime.fromisoformat(release.replace('Z','+00:00')):continue
  q=tag['name']+' rating:g solo'
  r=c.get('/counts/posts.json',params={'tags':q})
  row={'tag':tag,'query_url':str(r.url),'status':r.status_code,'response':r.json() if r.status_code==200 else r.text}
  rows.append(row);save_json_atomic(root/'candidates.json',{'release':release,'rows':rows,'elapsed':time.monotonic()-start})
  if r.status_code==200 and r.json()['counts']['posts']>=30:
   print(tag['name'],r.json()['counts']['posts'],tag['created_at'],flush=True)
  time.sleep(.4)
