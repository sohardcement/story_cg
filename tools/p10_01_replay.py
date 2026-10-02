"""Offline deliver on copies; prove original tasks and frozen files unchanged."""
import hashlib
import json
import shutil
from pathlib import Path
from storycg.agent import deliver
from storycg.state import save_json_atomic
from storycg.identity_enrichment import enrich_identity, load_rules

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'reports/assets/P10-01'


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root):
    return {str(p.relative_to(root)):hash_file(p) for p in sorted(root.rglob('*')) if p.is_file()}


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((ROOT/'calibration/v2/reviewer_freeze.json').read_text())['files']
    checks={p:hash_file(ROOT/p)==h for p,h in frozen.items()}
    assert all(checks.values()),checks
    import subprocess
    checks['reliability_unchanged']=not subprocess.check_output(['git','diff','--','config/review_reliability.json'],cwd=ROOT)
    assert checks['reliability_unchanged']
    results=[]
    for name in ('p9_06_cake','p9_07_ownership'):
        source=ROOT/'jobs'/name;target=OUTPUT/'replay/jobs'/name
        baseline=tree_hashes(source)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():shutil.rmtree(target)  # Only this script's copied artifacts.
        shutil.copytree(source,target)
        before=json.loads((source/'review_gate.json').read_text())
        deliver(target)
        after=json.loads((target/'review_gate.json').read_text())
        assert tree_hashes(source)==baseline
        assert hash_file(source/'review.json')==hash_file(target/'review.json')
        assert hash_file(source/'cycle.json')==hash_file(target/'cycle.json')
        original_state=json.loads((source/'state.json').read_text())
        for attempt in original_state['attempts']:
            if attempt.get('image'):
                image=attempt['image']['path']
                assert hash_file(source/image)==hash_file(target/image)
        # Offline rebuild is deterministic, including markdown and gate records.
        rebuilt={p:hash_file(target/p) for p in ('result.md','manifest.json','review_gate.json','contact_sheet.jpg')}
        deliver(target)
        assert rebuilt=={p:hash_file(target/p) for p in rebuilt}
        save_json_atomic(OUTPUT/f'{name}-original-hashes.json',baseline)
        results.append({'job':name,'before':before,'after':after,'original_tree_unchanged':True,
            'raw_review_unchanged':True,'image_bytes_unchanged':True,'deliver_idempotent':True,
            'delivery_dir':str(target.relative_to(ROOT))})
    identity=json.loads((ROOT/'jobs/p9_06_cake/identity.json').read_text())
    candidates=[]
    for path in (ROOT/'cache').rglob('*.json'):
        try:cache=json.loads(path.read_text())
        except (ValueError,UnicodeDecodeError):continue
        if isinstance(cache,dict) and 'related_tag.json' in cache.get('url','') and identity['character_tag'] in cache.get('url',''):
            candidates.append(cache)
    # Sources may also be persisted inside job caches.
    for path in (ROOT/'jobs/p9_06_cake').rglob('*.json'):
        try:cache=json.loads(path.read_text())
        except (ValueError,UnicodeDecodeError):continue
        if isinstance(cache,dict) and 'related_tag.json' in cache.get('url','') and identity['character_tag'] in cache.get('url',''):
            candidates.append(cache)
    if candidates:
        source=candidates[0]
        enriched=enrich_identity(identity,source['response'],source['url'],load_rules())
        save_json_atomic(OUTPUT/'yor-reextracted.json',enriched)
    save_json_atomic(OUTPUT/'replay-summary.json',{'jobs':results,'frozen_checks':checks,
        'usage':{'generated_images':0,'vision_calls':0,'llm_calls':0},
        'yor_cached_related_found':bool(candidates)})
    print(json.dumps({'jobs':[{ 'job':x['job'],'before':x['before']['conclusion'],
        'after':x['after']['conclusion'],'known_problems':len(x['after']['known_problems'])} for x in results],
        'freeze':all(checks.values()),'yor_cached_related_found':bool(candidates)},ensure_ascii=False))


if __name__=='__main__':main()
