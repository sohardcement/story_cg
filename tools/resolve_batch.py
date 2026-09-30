"""Resolve supplied names serially; no generation or visual model calls."""
import argparse
import json
import time
from pathlib import Path

import httpx
from storycg.identity import resolve_identity
from storycg.state import save_json_atomic


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('names',nargs='+')
    args=parser.parse_args()
    start=time.monotonic()
    manifest={'names':args.names,'generation_count':0,'vision_calls':0,'results':[]}
    for number,name in enumerate(args.names,1):
        directory=args.output/f'{number:02d}'
        begun=time.monotonic()
        try:
            result=resolve_identity(name,directory/'cache'/'danbooru')
        except (httpx.HTTPError,ValueError) as exc:
            result={'input_name':name,'status':'source_unavailable','error':str(exc),
                    'identity_tier':'tier_pending_comfyui','tier_evidence':{'generation_count':0,'vision_calls':0}}
        save_json_atomic(directory/'identity.json',result)
        manifest['results'].append({'input_name':name,'directory':str(directory),'status':result['status'],
                                    'elapsed_seconds':round(time.monotonic()-begun,3)})
        manifest['elapsed_seconds']=round(time.monotonic()-start,3)
        save_json_atomic(args.output/'manifest.json',manifest)
        print(json.dumps(manifest['results'][-1],ensure_ascii=False),flush=True)
    return 1 if any(x['status']=='source_unavailable' for x in manifest['results']) else 0


if __name__=='__main__':
    raise SystemExit(main())
