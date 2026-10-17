"""`storycg verify-llm`: the model acceptance suite (D113 series, collected into one command).

Does the local model behind `llm` write beats files the pipeline can take? Each story of inputs.json is written `--runs` times through the
same writer path as `write-beats` (guide + json_schema), with the repair loop of the D113 experiments: draft a1; if `compile --strict`
says invalid, or valid with warnings, the draft and the compiler's records go back to the model (a2, a3; at most 2 repair rounds).
Output, under verify/llm-acceptance/<model>/<date>/: beats/, calls.jsonl, loop.jsonl, results.json, results.md, page/index.html.

Pass lines (written here on purpose, see PASS_LINES): first draft >= 80%, after the loop >= 95%, blacklisted pose / out-of-enum action_tag
hits == 0, mean model seconds per document (all rounds) <= 60. Coverage of the story is a HUMAN item: the page is for it, the suite
never judges it (when every format line passes the page may be skipped).
Failure classes: a call error or timeout; b not JSON; c JSON but not the v3 beats schema (`compile --strict` schema stage: schema,
blacklisted pose, exclusive scene tags); d the schema passes and `compile` refuses. A document succeeds when none of a-d applies.
"""
import html
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_INPUTS = 'verify/v2/d113-storyboard/inputs.json'
PASS_LINES = {'first_draft': 0.80, 'after_loop': 0.95, 'blacklist_hits': 0, 'seconds_per_doc': 60}


def _log(path, row):
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _compile_strict(beats, character, shots):
    """`storycg compile --strict` in a subprocess; a Danbooru outage is retried (20 s apart, 3 tries), it is not the model's fault."""
    env = {'PYTHONPATH': str(PROJECT / 'src'), 'PATH': '/usr/bin:/bin', 'HOME': str(Path.home())}
    res = {}
    for _ in range(3):
        proc = subprocess.run([sys.executable, '-m', 'storycg', 'compile', '--strict', '--character', character, '--beats', str(beats),
                               '--shots', str(shots)], capture_output=True, text=True, cwd=PROJECT, env=env)
        try:
            res = json.loads(proc.stdout[proc.stdout.index('{'):])
        except ValueError:
            res = {'status': 'unparsed', 'message': (proc.stdout + proc.stderr)[-200:]}
        if res.get('status') in ('valid', 'invalid'):
            break
        time.sleep(20)
    return res


def _failure_class(res):
    """'' (none), 'b', 'c' or 'd' for a compile --strict result."""
    if res.get('status') == 'valid':
        return ''
    errors = res.get('errors') or []
    if any(e.get('field') == 'file' for e in errors):
        return 'b'
    return 'c' if res.get('message') == 'schema' else 'd'


def run(args):
    from storycg import llm
    from storycg.beats import beats_guide, character_guide
    from storycg.identity import resolve_identity
    from storycg.config import load_profile
    config = load_profile('default')
    config['llm'] = {**config.get('llm', {}), **{k: v for k, v in (('base_url', args.base_url), ('model', args.model)) if v}}
    cfg = llm.settings(config)
    llm.check_health(cfg)
    inputs = json.loads(Path(args.inputs).read_text(encoding='utf-8'))
    shots = inputs.get('shots', 4)
    out = Path(args.out) if args.out else PROJECT / 'verify' / 'llm-acceptance' / re.sub(r'[^\w.-]+', '_', cfg['model']) / (args.date or date.today().isoformat())
    (out / 'beats').mkdir(parents=True, exist_ok=True)
    guides = {}
    docs = []
    for run_no in range(1, args.runs + 1):
        for item in inputs['inputs']:
            character = item['character']
            if character not in guides:
                info = character_guide(resolve_identity(character, PROJECT / 'cache' / 'danbooru'), config)
                guides[character] = beats_guide(shots, info)
            base = out / 'beats' / f"{item['id']}-r{run_no}"
            previous = errors = None
            seconds = 0.0
            versions = []
            for rnd in (1, 2, 3):
                path = Path(f'{base}.a{rnd}.json')
                try:
                    text, rec = llm.write_beats(config, guides[character], character, item['story'], shots, previous=previous, errors=errors)
                except llm.LlmError as exc:
                    _log(out / 'calls.jsonl', {'id': item['id'], 'run': run_no, 'round': rnd, 'status': 'error', 'kind': exc.kind, 'message': str(exc)})
                    versions.append({'version': f'a{rnd}', 'cls': 'a', 'warnings': 0})
                    break
                path.write_text(text, encoding='utf-8')
                seconds += rec['seconds']
                _log(out / 'calls.jsonl', {'id': item['id'], 'run': run_no, 'round': rnd, 'status': 'written', **rec})
                res = _compile_strict(path, character, shots)
                warnings = res.get('warnings') or []
                cls = _failure_class(res) if res.get('status') in ('valid', 'invalid') else 'resource'
                versions.append({'version': f'a{rnd}', 'cls': cls, 'warnings': len(warnings)})
                _log(out / 'loop.jsonl', {'id': item['id'], 'run': run_no, 'version': f'a{rnd}', 'status': res.get('status'), 'class': cls,
                                          'errors': (res.get('errors') or [])[:6], 'warnings': warnings})
                if res.get('status') == 'invalid':
                    previous, errors = text, res['errors']
                elif res.get('status') == 'valid' and warnings and rnd < 3:
                    previous, errors = text, warnings
                else:
                    break
            final = versions[-1]
            if Path(f'{base}.a{len(versions)}.json').exists():
                Path(f'{base}.json').write_text(Path(f'{base}.a{len(versions)}.json').read_text(encoding='utf-8'), encoding='utf-8')
            docs.append({'id': item['id'], 'run': run_no, 'category': item.get('category'), 'character': character, 'story': item['story'],
                         'first_ok': versions[0]['cls'] == '', 'final_ok': final['cls'] == '', 'classes': [v['cls'] for v in versions],
                         'a1_warnings': versions[0]['warnings'], 'rounds': len(versions), 'llm_seconds': round(seconds, 1)})
            print(item['id'], run_no, [v['cls'] or 'ok' for v in versions], round(seconds, 1), flush=True)
    results = summarize(docs, out)
    (out / 'results.json').write_text(json.dumps({'model': cfg['model'], 'base_url': cfg['base_url'], 'results': results, 'docs': docs},
                                                 ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'results.md').write_text(table(cfg['model'], results), encoding='utf-8')
    make_page(out, inputs['inputs'], args.runs)
    print(table(cfg['model'], results))
    return 0 if results['auto_pass'] else 3


def summarize(docs, out):
    from storycg.beats import measured_action_tags
    from storycg.beats_v3 import POSE_BLOCK
    n = len(docs)
    enum = set(measured_action_tags())
    pose_hits = tag_hits = approx = 0
    for p in sorted((out / 'beats').glob('*.a?.json')):
        try:
            shots_ = json.loads(p.read_text(encoding='utf-8'))['shots']
        except (ValueError, KeyError, TypeError):
            continue
        for s in shots_ if isinstance(shots_, list) else []:
            if isinstance(s, dict):
                pose_hits += s.get('pose') in POSE_BLOCK
                tag_hits += bool(s.get('action_tag')) and s.get('action_tag') not in enum
    for p in sorted((out / 'beats').glob('*-r?.json')):
        try:
            approx += sum(1 for s in json.loads(p.read_text(encoding='utf-8'))['shots'] if isinstance(s, dict) and s.get('fit') == 'approx')
        except (ValueError, KeyError, TypeError):
            pass
    first = sum(d['first_ok'] for d in docs)
    final = sum(d['final_ok'] for d in docs)
    secs = [d['llm_seconds'] for d in docs]
    classes = {c: sum(d['classes'][0] == c for d in docs) for c in 'abcd'}
    res = {'documents': n, 'first_draft': first, 'first_draft_rate': round(first / n, 3), 'after_loop': final, 'after_loop_rate': round(final / n, 3),
           'first_draft_classes': classes, 'blacklist_pose_hits': pose_hits, 'out_of_enum_action_tag_hits': tag_hits,
           'a1_documents_with_warnings': sum(1 for d in docs if d['a1_warnings']), 'repair_rounds_used': sum(d['rounds'] - 1 for d in docs),
           'seconds_mean': round(sum(secs) / n, 1), 'seconds_max': max(secs), 'approx_shots_final': approx}
    res['lines'] = {'first_draft': res['first_draft_rate'] >= PASS_LINES['first_draft'], 'after_loop': res['after_loop_rate'] >= PASS_LINES['after_loop'],
                    'blacklist_and_enum_hits_zero': pose_hits + tag_hits == 0, 'seconds_per_doc': res['seconds_mean'] <= PASS_LINES['seconds_per_doc']}
    res['auto_pass'] = all(res['lines'].values())
    return res


def table(model, r):
    mark = lambda ok: 'PASS' if ok else 'FAIL'
    return '\n'.join([
        f'# verify-llm: {model}', '',
        '| 项 | 结果 | 线 | |', '|---|---|---|---|',
        f"| 一稿成功率 | {r['first_draft']}/{r['documents']} = {r['first_draft_rate']:.0%} | ≥ 80% | {mark(r['lines']['first_draft'])} |",
        f"| 回路后成功率 | {r['after_loop']}/{r['documents']} = {r['after_loop_rate']:.0%} | ≥ 95% | {mark(r['lines']['after_loop'])} |",
        f"| 黑名单 pose + 枚举外 action_tag 命中 | {r['blacklist_pose_hits']} + {r['out_of_enum_action_tag_hits']} | = 0 | {mark(r['lines']['blacklist_and_enum_hits_zero'])} |",
        f"| 单份模型耗时（含回路）平均 / 最慢 | {r['seconds_mean']} s / {r['seconds_max']} s | 平均 ≤ 60 s | {mark(r['lines']['seconds_per_doc'])} |",
        '| 剧情要点覆盖 | 人工项（见 page/index.html） | 不自动判定 | — |', '',
        f"一稿失败分类 a/b/c/d：{r['first_draft_classes']}；a1 带 warning 的份数 {r['a1_documents_with_warnings']}；用掉的修复轮 {r['repair_rounds_used']}；最终版 approx 镜头 {r['approx_shots_final']}。",
        f"自动项：{'全部通过，可以直接接 Hermes（覆盖分另判）' if r['auto_pass'] else '有项未过，不能直接接 Hermes'}", ''])


def render(path):
    """One beats file as plain text, the way it will be drawn (an approx action_tag is shown dropped)."""
    from storycg.beats_v3 import APPROX_NOTE
    if not path.exists():
        return '(没有这份文件)'
    raw = path.read_text(encoding='utf-8')
    try:
        d = json.loads(raw)
        scenes, outfits, lines = d.get('scenes') or {}, d.get('outfits') or {}, []
        if d.get('story_note'):
            lines.append('说明：' + str(d['story_note']))
        for n, s in enumerate(d['shots'], 1):
            approx = s.get('fit') == 'approx' or bool(s.get('action_tag') and APPROX_NOTE.search(str(s.get('action_note') or '')))
            sc, of = scenes.get(s.get('scene'), {}), s.get('outfit')
            outfit = '标准服装' if of in (None, 'standard') else f"{of}（{', '.join((outfits.get(of) or {}).get('tags', []))}）"
            extra = ', '.join(f'{k}={v}' for k, v in s.items() if k in ('action_tag', 'prop_tags') and v and not (approx and k == 'action_tag'))
            lines.append(f"第{n}镜  场景[{s.get('scene')}]：{', '.join(sc.get('tags', [])) if isinstance(sc, dict) else '?'}\n      服装：{outfit}\n"
                         f"      动作：{s.get('pose')}  表情：{s.get('expression')}  景别/角度：{s.get('framing')}/{s.get('camera_angle')}"
                         + (f"\n      其他：{extra}" if extra else ''))
        return '\n'.join(lines)
    except (ValueError, KeyError, TypeError, AttributeError):
        return '(不是可读的 JSON，原文前 800 字)\n' + raw[:800]


def make_page(out, inputs, runs):
    opts = lambda xs: ''.join(f'<option>{x}</option>' for x in xs)
    page = ['<!doctype html><meta charset="utf-8"><title>verify-llm 覆盖分</title><style>body{font:14px sans-serif;max-width:1300px;margin:20px auto}'
            '.c{border-top:3px solid #444;margin-top:24px;padding-top:8px}.g{display:grid;grid-template-columns:repeat(%d,1fr);gap:10px}.d{border:1px solid #bbb;padding:6px}'
            'pre{white-space:pre-wrap;font:12px/1.45 monospace}</style>' % runs,
            '<h2>verify-llm：覆盖分（人工项）</h2><p>(a) 要点覆盖 0 没覆盖 / 1 部分 / 2 覆盖（如果把这几镜画出来，剧情要点能不能看出来；两人对话只看主角）。'
            '(b) 连续性错误：场景、时间、服装有没有无故突然变。(d) 覆盖分是否被一个会画错的 pose 或 action_tag 拉低。</p>']
    for item in inputs:
        page.append(f'<div class="c" data-id="{item["id"]}"><h3>{item["id"]}　{html.escape(str(item.get("category", "")))}　{html.escape(item["character"])}</h3>'
                    f'<p><b>剧情：</b>{html.escape(item["story"])}</p><div class="g">')
        for r in range(1, runs + 1):
            page.append(f'<div class="d" data-doc="r{r}"><b>r{r}</b><pre>{html.escape(render(out / "beats" / f"{item["id"]}-r{r}.json"))}</pre>'
                        f'(a) <select data-k="cover">{opts(["未选", "0", "1", "2"])}</select> (b) <select data-k="cont">{opts(["未选", "是", "否"])}</select> '
                        f'(d) <select data-k="badtag">{opts(["未选", "是", "否"])}</select></div>')
        page.append('</div></div>')
    page.append('<p><button onclick="copyAll()">复制判定</button> <span id="m"></span></p><script>function copyAll(){const o={};'
                'document.querySelectorAll(".c").forEach(c=>{const r={};c.querySelectorAll(".d").forEach(d=>{const x={};'
                'd.querySelectorAll("[data-k]").forEach(e=>x[e.dataset.k]=e.value);r[d.dataset.doc]=x});o[c.dataset.id]=r});'
                'navigator.clipboard.writeText(JSON.stringify(o,null,1)).then(()=>document.getElementById("m").textContent="已复制")}</script>')
    (out / 'page').mkdir(exist_ok=True)
    (out / 'page' / 'index.html').write_text('\n'.join(page), encoding='utf-8')
