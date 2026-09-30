"""Supplementary diagnosis; never regenerates or replaces the initial localization."""
import json,sys
from pathlib import Path
from PIL import Image
from storycg.repairs import region_queries,region_plan
from storycg.review import VisionClient,messages,BBOX_SCHEMA,normalize_bbox
from storycg.state import load_state,save_json_atomic
cases=json.loads(Path('reports/assets/P5-01b/cases.json').read_text())
c=cases[0];root=Path(c['destination']);st=load_state(root);board=json.loads((root/'storyboard.json').read_text());entry=board['shots'][c['shot']-1]
source=st['repairs']['shot03_repair1']['source_image'];path=root/source['path'];op={'type':'remove','prop_ids':[p['prop_id'] for p in entry['shot']['key_props']]}
output=root/('region_diagnosis_distinct.json' if '--distinct' in sys.argv else 'region_diagnosis_corrected.json')
if output.exists():raise SystemExit('existing supplementary diagnosis retained')
v=VisionClient(root,st['config']['model_service_url'],max_calls=4);answers=[];boxes=[]
for query in region_queries(entry,board['props'],op):
 answer=v.call('supplementary_'+query['kind']+'_'+query['prop_id'],messages(query['query']+' Ground truth shot: '+json.dumps(entry['shot'])+' Registered objects: '+json.dumps(board['props'])+' Return normalized [left,top,right,bottom].',[path]),BBOX_SCHEMA,[path]);answers.append({**query,'answer':answer})
 if answer['status']=='pass' and answer['data']['found']:boxes.append(normalize_bbox(answer['data']['bbox'],path)[0])
v.close()
with Image.open(path) as im:plan=region_plan(boxes,im.size,st['config']['repair_settings']['inpaint']['max_area_fraction'],st['config']['repair_settings']['inpaint']['context_margin']) if boxes else None
save_json_atomic(output,{'purpose':'supplementary diagnosis only, after the already completed generation; initial evidence unchanged', 'diagnosis':{'diagnosis':'Different registered objects collapsed to identical object bboxes in the previous localization.', 'hypothesis':'Class-only queries cannot distinguish same-kind instances.', 'changes':'Derive distinctive attributes by subtracting other registered objects description tags; query each matching instance separately.'},'answers':answers,'plan':plan})
print(json.dumps(plan),flush=True)
