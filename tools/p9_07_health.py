"""Read-only service probe; never changes remote service state."""
import json,sys
from datetime import datetime,timezone
from pathlib import Path
import httpx
cfg=json.loads(Path('config/default.json').read_text())
result={'at':datetime.now(timezone.utc).isoformat(),'services':{}}
for name,url in [('comfyui',cfg['comfyui_url']+'/queue'),('vision',cfg['model_service_url']+'/models')]:
    try:
        r=httpx.get(url,timeout=8,trust_env=False)
        result['services'][name]={'url':url,'status':r.status_code,'body':r.json() if r.status_code==200 else r.text[:300]}
    except Exception as exc:result['services'][name]={'url':url,'error':str(exc)}
Path(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
sys.exit(0 if all(s.get('status')==200 for s in result['services'].values()) else 1)
