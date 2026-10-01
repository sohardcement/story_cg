"""Read-only receipts for a work order; no service calls or generation."""
import argparse
import hashlib
import json
from pathlib import Path
from storycg.agent import status

parser=argparse.ArgumentParser()
parser.add_argument('jobs',nargs='+',type=Path)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()

def read(path):return json.loads(path.read_text()) if path.exists() else None

rows=[]
for root in args.jobs:
    root=root.resolve()
    rows.append({'task':str(root),'status':status(root),'manifest':read(root/'manifest.json'),
                 'review':read(root/'review.json'),'portrait':read(root/'portrait.json'),
                 'portrait_attempts':read(root/'portrait_attempts.json'),
                 'files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in root.rglob('*') if p.is_file() and p.suffix in ('.png','.jpg')}})
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
