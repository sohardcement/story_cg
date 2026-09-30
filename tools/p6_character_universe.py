import json,time
from pathlib import Path
import httpx
from storycg.tiers import _frequencies
from storycg.identity import USER_AGENT
from storycg.state import save_json_atomic
root=Path('reports/assets/P6-01b')
metadata=json.loads((root/'lora-metadata-live.json').read_text())
tags=sorted({t for m in metadata.values() for t in _frequencies(m)})
records=[]
with httpx.Client(base_url='https://danbooru.donmai.us',timeout=30,trust_env=False,headers={'User-Agent':USER_AGENT}) as client:
 for index in range(0,len(tags),80):
  r=client.get('/tags.json',params={'search[name_comma]':','.join(tags[index:index+80]),'limit':100});r.raise_for_status()
  records.append({'url':str(r.url),'response':r.json()});save_json_atomic(root/'tag-categories.json',records)
  print(index,len(tags),flush=True);time.sleep(.8)
characters=sorted({t['name'] for record in records for t in record['response'] if t['category']==4})
save_json_atomic(root/'character-universe.json',characters)
print('characters',len(characters))
