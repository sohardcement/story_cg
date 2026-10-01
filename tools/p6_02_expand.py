"""Continue the same date-ordered search until usable candidates are found."""
import json,time
from pathlib import Path
from datetime import datetime
import httpx
from storycg.identity import USER_AGENT,resolve_identity
from storycg.tiers import download_references
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-02')
release=json.loads((root/'release.json').read_text())['body']['publishedAt'];cutoff=datetime.fromisoformat(release.replace('Z','+00:00'))
rows=json.loads((root/'candidates.json').read_text())['rows'];seen={r['tag']['name'] for r in rows};eligible=[]
with httpx.Client(base_url='https://danbooru.donmai.us',timeout=40,trust_env=False,headers={'User-Agent':USER_AGENT}) as c:
 for page in range(1,6):
  r=c.get('/tags.json',params={'search[category]':4,'search[order]':'date','search[post_count]':'>=30','limit':100,'page':page});r.raise_for_status();tags=r.json()
  save_json_atomic(root/f'tags-page-{page}.json',{'url':str(r.url),'response':tags})
  for tag in tags:
   if datetime.fromisoformat(tag['created_at'])<=cutoff:continue
   record=next((x for x in rows if x['tag']['name']==tag['name']),None)
   if record is None:
    r=c.get('/counts/posts.json',params={'tags':tag['name']+' rating:g solo'});r.raise_for_status()
    record={'tag':tag,'query_url':str(r.url),'status':r.status_code,'response':r.json()};rows.append(record)
    save_json_atomic(root/'candidates-expanded.json',{'release':release,'rows':rows})
   if record['response'].get('counts',{}).get('posts',0)<30:continue
   job=Path('runs/p6_02/candidates')/tag['name'].replace('/','_');ip=job/'identity.json'
   try:
    ident=json.loads(ip.read_text()) if ip.exists() else resolve_identity(tag['name'],job/'cache',client=c,character_tag=tag['name'])
    save_json_atomic(ip,ident)
    hair=[x['tag'] for x in ident.get('appearance_tags',[]) if x['category']=='hair_color']
    if not hair:print(tag['name'],'no reliable hair_color source',flush=True);continue
    probe=job/'tier'/('probe_'+job.name);mp=probe/'references_v2/manifest.json'
    refs=json.loads(mp.read_text()) if mp.exists() else download_references(tag['name'],probe,c,identity=ident)
    print(tag['name'],hair,'refs',len(refs),flush=True)
    if len([x for x in refs if x.get('status')=='downloaded'])>=2:
     eligible.append({'tag':tag['name'],'created_at':tag['created_at'],'g_solo_count':record['response']['counts']['posts'],'job':str(job),'hair':hair,'references':len(refs)})
     save_json_atomic(root/'usable-candidates.json',eligible)
     if len(eligible)>=7:raise SystemExit(0)
   except (httpx.HTTPError,ValueError,KeyError) as e:print(tag['name'],type(e).__name__,str(e),flush=True)
