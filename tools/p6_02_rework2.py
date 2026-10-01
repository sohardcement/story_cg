"""Offline D8b summary of existing evidence; no generation/network/vision calls.

Only writes new receipts to reports/assets/P6-02-rework2. Original observations,
workflows, identities, reference files, and images are immutable inputs.
"""
import hashlib
import json
from pathlib import Path
from storycg.state import save_json_atomic
from storycg.tiers import reaggregate_stored_tier
from storycg.fallback import current_l3_approval


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    assets=Path('reports/assets/P6-02-rework2')
    ledger=json.loads(Path('reports/assets/P6-02-rework1/usage-final.json').read_text())
    reliability=json.loads(Path('config/review_reliability.json').read_text())
    roots=[Path('runs/p6_02_rework1'),Path('runs/p6_01b')]
    inputs={str(p):digest(p) for root in roots for p in root.rglob('*') if p.is_file()}
    old_l1={}
    for path in Path('runs/p6_01b').glob('*/identity.json'):
        identity=json.loads(path.read_text())
        if identity.get('identity_tier')=='L1':old_l1[identity['character_tag']]=path
    results=[]
    for row in ledger['results']:
        job=Path(row['job']);source=job/'knowledge-result.json'
        original=json.loads((job/'original-identity.json').read_text())
        if original.get('lora'):
            source=old_l1[row['tag']]
        identity=json.loads(source.read_text())
        revised=reaggregate_stored_tier(source.parent,identity,reliability)
        assert revised is not None, f'Invalid protocol or evidence: {source}'
        probe_tier=revised['identity_tier']
        if row['tier']=='L3':
            approved=json.loads((job/'portrait.json').read_text())['approved_identity']
            assert current_l3_approval(approved)
            aggregate=revised['tier_evidence']
            revised=approved
            for key in ('reliable','unverified','outfit_comparisons','vetoes','not_visible','limitations','aggregation_policy','tier_scope','probe_protocol'):
                revised['tier_evidence'][key]=aggregate[key]
        out=assets/'roles'/f'{row["index"]:02d}.json'
        save_json_atomic(out,{'source_identity':str(source),'source_sha256':inputs[str(source)],
                             'probe_tier_D8b':probe_tier,'identity':revised})
        e=revised['tier_evidence'] if revised['identity_tier']!='L3' else reaggregate_stored_tier(source.parent,identity,reliability)['tier_evidence']
        record={'tag':row['tag'],'D8':row['tier'],'D8b':revised['identity_tier'],
                'probe_D8b':probe_tier,'source_identity':str(source),'receipt':str(out),
                'appearance_vetoes':sorted({r['tag'] for r in e['vetoes']}),
                'reliable_unconfirmed':[r['tag'] for r in e['reliable'] if not r['passed']],
                'not_visible':[{k:r.get(k) for k in ('item','tag','status')} for r in e['not_visible']],
                'outfit_diagnostics':e['outfit_comparisons'],
                'raw_observations_sha256':hashlib.sha256(json.dumps(identity['tier_evidence']['comparisons'],sort_keys=True).encode()).hexdigest()}
        assert revised['tier_evidence']['comparisons']==identity['tier_evidence']['comparisons']
        results.append(record)
    assert all(Path(p).is_file() and digest(Path(p))==value for p,value in inputs.items())
    assert set(inputs)=={str(p) for root in roots for p in root.rglob('*') if p.is_file()}
    save_json_atomic(assets/'input-integrity.json',{'unchanged':True,'files':inputs})
    save_json_atomic(assets/'summary.json',{'policy':'D8b','generated_images':0,'vision_calls':0,
                                         'all_original_inputs_unchanged':True,'results':results})
    table=['| 角色 tag | D8 | D8b | 依据 / 剩余问题 |','|---|---|---|---|']
    for r in results:
        reason='外貌 fail：'+', '.join(r['appearance_vetoes']) if r['appearance_vetoes'] else '可靠外貌全 pass，无外貌 fail'
        if r['reliable_unconfirmed']:reason+='；可靠项未确认：'+', '.join(r['reliable_unconfirmed'])
        if r['D8b']=='L3':reason+='；保留已通过的 L3 定妆'
        elif r['source_identity'].startswith('runs/p6_01b'):reason+='；复用 P6-01b 专用 LoRA + 来源外貌验证'
        if r['D8b']=='tier_pending_L3_L4':reason+='；尚无通过的 L3 定妆，待 L3'
        display=lambda t:'待 L3' if t=='tier_pending_L3_L4' else t
        table.append(f'| `{r["tag"]}` | {display(r["D8"])} | {display(r["D8b"])} | {reason} |')
    (assets/'comparison-table.md').write_text('\n'.join(table)+'\n')
    print('\n'.join(table))
    print(f'Immutable inputs: {len(inputs)}; generated images: 0; vision calls: 0')


if __name__=='__main__':main()
