"""Read persisted matrix evidence; never submit generation/review requests."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = ['runs/p5_02_a_r1', 'runs/p5_02_b_r1', 'jobs/cg-9b3e05ab005b',
        'jobs/cg-76d2eec57dfc', 'jobs/cg-28fefea56956', 'jobs/cg-672a3a6c995b']

def read(path):
    return json.loads(path.read_text()) if path.exists() else {}

for directory in DIRS:
    p = ROOT / directory
    identity = read(p / 'identity.json')
    manifest = read(p / 'manifest.json')
    review = read(p / 'review.json')
    agent = read(p / 'agent.json')
    status = {}
    if agent:
        completed = subprocess.run([sys.executable, '-m', 'storycg', 'status', str(p)], cwd=ROOT, capture_output=True, text=True, check=False)
        status = json.loads(completed.stdout)
        status['command_exit_code'] = completed.returncode
    result = {
        'directory': directory, 'phase': agent.get('phase'),
        'tier': identity.get('identity_tier'),
        'tier_summary': identity.get('tier_evidence', {}).get('reason'),
        'usage': manifest.get('usage') or status.get('usage'),
        'final_status_usage': status.get('usage'),
        'error': agent.get('error'),
        'conclusion': review.get('conclusion'),
        'blind': review.get('narrative_blind'),
        'compare': review.get('narrative_compare'),
        'images': manifest.get('images'),
    }
    print(json.dumps(result, ensure_ascii=False))
