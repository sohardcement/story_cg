"""Offline contract, immutable inputs, real output provenance and budget checks."""
import hashlib
import json
from pathlib import Path
from storycg.budget import count_usage
from storycg.planning import validate_storyboard

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'reports/assets/P9-05'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    checks={};before=json.loads((ASSETS/'frozen-before.json').read_text())
    checks['frozen_files']={p:digest(ROOT/p)==sha for p,sha in before.items()}
    old=json.loads((ASSETS/'old-work-hashes.json').read_text())
    checks['old_works']={root:all((ROOT/root/p).is_file() and digest(ROOT/root/p)==sha for p,sha in files.items()) for root,files in old.items()}
    checks['jobs']=[]
    for spec in json.loads((ASSETS/'regressions.json').read_text()):
        root=ROOT/spec['job'];job=json.loads((root/'job.json').read_text());agent=json.loads((root/'agent.json').read_text())
        assert agent['phase'] in ('completed','stopped','needs_user'),agent['phase']
        usage=count_usage(root);assert usage['submission_attempts']<=40 and usage['generated_images']<=40
        assert usage['elapsed_seconds']<=3600 and usage['wall_elapsed_seconds']<=3600,usage
        completed=[]
        for state_path in root.rglob('state.json'):
            state=json.loads(state_path.read_text())
            for attempt in state.get('attempts',[]):
                if attempt.get('status')!='completed':continue
                image=state_path.parent/attempt['image']['path']
                assert attempt.get('prompt_id'),str(state_path)
                assert root.name in image.name,str(image)
                assert digest(image)==attempt['image']['sha256'],str(image)
                completed.append({'path':str(image.relative_to(ROOT)),'prompt_id':attempt['prompt_id'],'sha256':attempt['image']['sha256']})
        assert len(completed)==usage['generated_images'],(len(completed),usage)
        board_path=root/'storyboard.json'
        if board_path.exists():
            board=json.loads(board_path.read_text());assert board['schema_version']==2
            validate_storyboard(board,job['shot_count'],json.loads((root/'identity.json').read_text()))
        manifest_path=root/'manifest.json';images=[]
        if manifest_path.exists():
            manifest=json.loads(manifest_path.read_text())
            for im in manifest.get('images',[]):
                assert digest(root/im['path'])==im['sha256']
                images.append(im['path'])
        preview=root/'contact_sheet.jpg'
        if preview.exists():
            target=ROOT/'review'/f"{spec['name']}-{spec['short']}-v2.jpg"
            assert digest(preview)==digest(target)
        checks['jobs'].append({'job':spec['job'],'phase':agent['phase'],'usage':usage,'selected_images':images,'fresh_completed_generations':completed})
    raw_path=ASSETS/'raw-review-before-final-delivery.json'
    if raw_path.exists():
        checks['raw_review_unchanged']={p:digest(ROOT/p)==sha for p,sha in json.loads(raw_path.read_text()).items()}
    assert all(checks['frozen_files'].values()) and all(checks['old_works'].values())
    assert all(checks.get('raw_review_unchanged',{}).values())
    checks['total_generated_images']=sum(j['usage']['generated_images'] for j in checks['jobs'])
    assert checks['total_generated_images']<=120
    checks['status']='pass'
    (ASSETS/'validation.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    print(json.dumps(checks,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
