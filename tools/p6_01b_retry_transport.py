"""Retry only failed transport comparisons; retain original errors for audit."""
import json
from pathlib import Path
from storycg.state import save_json_atomic
for path in Path('runs/p6_01b').glob('*/identity.json'):
 identity=json.loads(path.read_text());evidence=identity['tier_evidence']
 if evidence.get('validation_completed') and all('is not in the subpath' in pair['result'].get('error','') for pair in evidence.get('comparisons',[])):
  save_json_atomic(path.parent/'tier/transport-error-original.json',evidence)
  evidence['validation_completed']=False
  evidence['retry_diagnosis']={'diagnosis':'VisionClient logging rejected absolute artifact paths against relative job_dir before HTTP call',
    'hypothesis':'absolute job_dir resolves logging boundary without changing prompts or review criteria',
    'changes':'resolve tier job root before constructing VisionClient; repeat comparison on same new probe, no regeneration'}
  save_json_atomic(path,identity)
# The initial candidate page did not contain two eligible references. Pagination
# is now part of the generic downloader, retaining the same rating/score filters.
for path in Path('runs/p6_01b').glob('*/tier/probe_*/references/manifest.json'):
 refs=json.loads(path.read_text())
 if len(refs)<3:
  save_json_atomic(path.parent/'first-page-manifest.json',refs)
  path.rename(path.parent/'first-page-insufficient-manifest.json')
