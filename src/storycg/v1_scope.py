"""D23–D25 scope policy; consumes structured state, never guesses from prose."""
import copy
from pathlib import Path
import json

PROP_NOTICE='本剧情的关键情节依赖道具归属/状态，v1 不保证能从画面读出'


def is_prop_item(key):
    return key == 'props' or key.startswith('prop_')


def person_findings(result):
    filtered=copy.deepcopy(result)
    filtered['data']={k:v for k,v in result.get('data',{}).items() if not is_prop_item(k)}
    return filtered


def scope_context(root):
    root=Path(root)
    def read(name):
        path=root/name
        return json.loads(path.read_text()) if path.exists() else {}
    intake=read('agent.json').get('intake',{})
    board=read('storyboard.json')
    # possession_swap was already a semantic LLM judgment, not a keyword guess.
    dependent=intake.get('prop_dependent',board.get('prop_dependent'))
    if dependent is None and (intake.get('possession_swap') or board.get('possession_swap')):
        dependent=True
    return {'prop_dependent':dependent,
            'prop_dependency_reason':intake.get('prop_dependency_reason',board.get('prop_dependency_reason','')),
            'prop_only_narrative_items':intake.get('prop_only_narrative_items',board.get('prop_only_narrative_items',[]))}


def narrative_findings(result, context=None):
    # Frozen aggregate narrative items mix person and prop semantics. Only intake-declared
    # pure-prop requirements are excluded; mixed person requirements remain active.
    filtered=copy.deepcopy(result)
    excluded=set((context or {}).get('prop_only_narrative_items',[])) if (context or {}).get('prop_dependent') is True else set()
    filtered['data']={k:v for k,v in result.get('data',{}).items() if k not in excluded}
    return filtered


def delivery_scope_notes(root):
    """Fixed v2 delivery notice plus the saved storyboard adaptation, if any."""
    path = Path(root)/'storyboard.json'
    board = json.loads(path.read_text()) if path.exists() else {}
    note = board.get('story_note', '').strip()
    identity_path = Path(root)/'identity.json'
    identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
    cache = identity.get('tier_cache', {})
    receipt_path = Path(root)/'tier/cache-reuse.json'
    if not cache.get('hit') and receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('key_inputs') == identity.get('tier_model_fingerprint'):
            cache = receipt
    tier_note = ['层级沿用缓存（'+cache['established_at_utc'][:10]+'）。'] if cache.get('hit') else []
    return ['道具未经审核。'] + ([note] if note else []) + tier_note


def render_delivery(root, report, gate, config=None):
    """Replace the explanation from saved evidence, avoiding stale failure headings."""
    root=Path(root);context=scope_context(root)
    lines=['# 结果说明','', '整组结论（v1 人物范围）：'+gate['conclusion'],
           '冻结原始审核结论：'+str(gate['raw_conclusion'])+'（包含 v1 不保证的项目；原始记录未改）',
           '', '## 已知问题','', *delivery_scope_notes(root)]
    if context['prop_dependent'] is True:lines.append('- '+PROP_NOTICE+'。')
    if context['prop_dependent'] is None:
        lines.append('- 旧任务缺少结构化道具依赖判断；混合叙事结论保守保留，不进行关键词推断。')
    lines.extend('- '+note for note in gate['known_problems'])
    for layer in ('single','consistency'):
        for row in report.get(layer,[]):
            if row.get('status')!='pass':lines.append(f'- {layer} shot {row.get("shot")}: 审核调用失败；'+row.get('error','unknown'))
            for key,item in row.get('data',{}).items():
                if not is_prop_item(key) and item.get('status')!='pass':
                    lines.append(f'- {layer} shot {row.get("shot")}: {key}: {item.get("status")} — {item.get("reason","")}')
                if item.get('reliability') not in (None,'reliable'):
                    lines.append(f'- {key}: 该项未经可靠审核。')
    for key,item in report.get('narrative_compare',{}).get('data',{}).items():
        if item.get('status')!='pass':lines.append(f'- 盲审 {key}: {item.get("status")} — {item.get("reason","")}（原始记录）')
    lines.extend('- '+n for n in gate['notes'])
    from storycg.review import reliability_note_lines
    lines.extend(reliability_note_lines(report,config))
    lines.append('- 远处小人物可能漏检；配饰/发型等未可靠校准的项需要看图确认。')
    for name in ('portrait.json','cycle.json'):
        path=root/name
        if not path.exists():continue
        record=json.loads(path.read_text())
        lines.extend('- '+n for n in record.get('gate_notes',[]))
        if record.get('blocked'):lines.append('- 修复阻塞：'+record['blocked'])
        for number,slot in record.get('shots',{}).items():
            if slot.get('diagnosis_error'):lines.append(f'- 第{number}镜历史诊断拒绝：'+slot['diagnosis_error'])
    human_path=root/'human_observations.json'
    if human_path.exists():
        for number,notes in json.loads(human_path.read_text()).items():
            lines.extend(f'- 第{number}镜人眼已知问题：'+n for n in notes)
    lines += ['', '审核与修复历史见 manifest.json、cycle.json、review.json；本说明没有重新审核图片。']
    (root/'result.md').write_text('\n'.join(lines)+'\n')
