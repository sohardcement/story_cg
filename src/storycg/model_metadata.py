"""Per-file metadata cache. Missing headers are ordinary inventory observations."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import httpx
from storycg.state import save_json_atomic
from storycg.runner import _job_lock


def cached_lora_metadata(config, client, names):
    cache_path=Path(config.get('lora_metadata_cache',Path(__file__).resolve().parents[2]/'cache/lora_metadata.json'))
    files={}
    for file in client.list_model_files('loras'):files.setdefault(file['name'],file)
    source=config['comfyui_url'].rstrip('/')
    metadata={};statuses={}
    with _job_lock(cache_path.parent):
        cache=json.loads(cache_path.read_text()) if cache_path.exists() else {'version':1,'servers':{}}
        records=cache['servers'].setdefault(source,{})
        pending=[]
        for name in names:
            file=files.get(name,{})
            stamp={'mtime':file.get('modified'),'size':file.get('size')}
            record=records.get(name)
            hit=record is not None and record.get('fingerprint')==stamp and all(v is not None for v in stamp.values())
            if hit:
                metadata[name]=record['data'];statuses[name]={k:v for k,v in record.items() if k!='data'};statuses[name]['cache_hit']=True
            else:pending.append((name,stamp))
        def fetch(name,stamp):
            try:
                data=client.get_model_metadata('loras',name)
                if not isinstance(data,dict):raise ValueError('metadata is not an object')
                return {'fingerprint':stamp,'metadata':'available','data':data}
            except httpx.HTTPStatusError as exc:
                return {'fingerprint':stamp,'metadata':'unavailable','data':{},'reason':f'HTTP {exc.response.status_code}'}
            except httpx.RequestError as exc:
                health=client.http.get('/system_stats',timeout=15);health.raise_for_status()
                return {'fingerprint':stamp,'metadata':'unavailable','data':{},'reason':f'{type(exc).__name__}: {exc}'}
            except (ValueError,TypeError) as exc:
                return {'fingerprint':stamp,'metadata':'unavailable','data':{},'reason':str(exc)}
        # Four passive metadata requests; generation submission remains serial.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures={executor.submit(fetch,name,stamp):name for name,stamp in pending}
            for future in as_completed(futures):
                name=futures[future];record=future.result();records[name]=record
                save_json_atomic(cache_path,cache)
                metadata[name]=record['data'];statuses[name]={k:v for k,v in record.items() if k!='data'};statuses[name]['cache_hit']=False
    return metadata,statuses
