import json,time
from pathlib import Path
from storycg.identity import resolve_identity
from storycg.state import save_json_atomic
rows=json.loads(Path('reports/assets/P6-02/candidates.json').read_text())['rows']
for row in rows:
 if not isinstance(row['response'],dict) or row['response'].get('counts',{}).get('posts',0)<30:continue
 tag=row['tag']['name'];root=Path('runs/p6_02/candidates')/tag.replace('/','_')
 start=time.monotonic()
 try:
  result=resolve_identity(tag,root/'cache',character_tag=tag)
  save_json_atomic(root/'identity.json',result)
  print(tag,result['status'],result.get('identity_block'),result.get('outfit_block'),flush=True)
 except Exception as e:print(tag,type(e).__name__,str(e),flush=True)
