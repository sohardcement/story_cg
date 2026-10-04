#!/usr/bin/env python3
"""Independently check saved audit data without importing production modules."""
import ast
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audit'
COHORTS = ('jobs_cli', 'jobs_development', 'runs_development')


def read(name):
    return json.loads((OUT / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def group(path):
    parts = path.relative_to(ROOT).parts
    return ('jobs_cli' if parts[1].startswith('cg-') else 'jobs_development') if parts[0] == 'jobs' else 'runs_development'


def main():
    checks = {}
    static = read('static.json')
    stats = read('log_stats.json')
    modules = read('module_evidence.json')
    summary = read('summary.json')
    evidence = read('validation_evidence.json')
    inputs = read('input_files.json')
    events = [json.loads(line) for line in (OUT / 'events.jsonl').read_text().splitlines()]

    # Compile in memory only: no imports, API calls, or __pycache__ writes.
    audit_scripts = sorted(OUT.glob('*.py'))
    for path in audit_scripts:
        compile(path.read_text(), str(path), 'exec')
    checks['audit_scripts_compile_in_memory'] = len(audit_scripts)

    sources = sorted((ROOT / 'src/storycg').glob('*.py'))
    expected_files = {str(path.relative_to(ROOT)) for path in sources}
    assert expected_files == {row['file'] for row in modules}
    assert len(modules) == len(expected_files) == summary['module_count']
    assert expected_files == {row['file'] for row in static['modules']}
    static_by_file = {row['file']: row for row in static['modules']}
    definition_locations = set()
    definition_count = 0
    total_lines = 0
    for path in sources:
        name = str(path.relative_to(ROOT))
        raw = path.read_bytes()
        nlines = len(raw.splitlines())
        total_lines += nlines
        assert static_by_file[name]['sha256'] == digest(path)
        assert static_by_file[name]['lines'] == nlines
        tree = ast.parse(raw, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definition_count += 1
                definition_locations.add((name, node.lineno, node.end_lineno))
    assert definition_count == len(static['functions'])
    assert definition_locations == {(row['file'], row['line'], row['end']) for row in static['functions']}
    assert total_lines == summary['lines'] == sum(row['lines'] for row in modules)
    recommendations = Counter()
    for row in modules:
        assert row['lines'] == static_by_file[row['file']]['lines']
        assert row['suggestion'] in ('保留', '简化', '删除')
        recommendations[row['suggestion']] += row['lines']
    assert dict(recommendations) == summary['recommendation_lines']
    checks['modules_and_source_hashes'] = len(sources)
    checks['source_lines'] = total_lines
    checks['definition_locations'] = definition_count

    # Graph closure independent of the scanner's implementation.
    adj = defaultdict(set)
    symbols = {row['symbol'] for row in static['functions']}
    for edge in static['edges']:
        assert edge['caller'] in symbols and edge['callee'] in symbols
        adj[edge['caller']].add(edge['callee'])
    for label, root, field in [('facade', 'storycg.agent.main', 'facade_reachable'),
                               ('all_cli', 'storycg.__main__.main', 'all_cli_reachable')]:
        reach, pending = set(), [root]
        while pending:
            name = pending.pop()
            if name not in reach:
                reach.add(name)
                pending.extend(adj[name])
        assert reach == {row['symbol'] for row in static['functions'] if row[field]}
        checks[label + '_reachable_definitions'] = len(reach)
    listed_dead = set(summary['test_only_symbols'] + summary['tools_only_symbols'] + summary['unreferenced_symbols'])
    assert listed_dead == {row['symbol'] for row in static['functions'] if not row['all_cli_reachable']}
    checks['classified_unreachable_definitions'] = len(listed_dead)

    for name, meta in inputs.items():
        path = ROOT / name
        assert path.is_file(), name
        assert path.stat().st_size == meta['bytes'], name
        assert digest(path) == meta['sha256'], name
    checks['unchanged_scanned_input_files'] = len(inputs)
    assert not stats['parse_errors']

    # Every evidence event must resolve to a real JSON value or existing log line.
    documents, lines = {}, {}
    counts = defaultdict(Counter)
    paths = defaultdict(lambda: defaultdict(set))
    tasks = defaultdict(lambda: defaultdict(set))
    values = defaultdict(Counter)
    for entry in events:
        name, pointer = entry['path'], entry['pointer']
        path = ROOT / name
        assert entry['cohort'] == group(path), entry
        if pointer.startswith('line:'):
            if name not in lines:
                lines[name] = path.read_text(errors='replace').splitlines()
            index = int(pointer.split(':', 1)[1]) - 1
            assert 0 <= index < len(lines[name]), entry
        else:
            if name not in documents:
                documents[name] = json.loads(path.read_text())
            value = documents[name]
            if pointer:
                assert pointer.startswith('/'), entry
                for part in pointer[1:].split('/'):
                    part = part.replace('~1', '/').replace('~0', '~')
                    try:
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    except (KeyError, IndexError, ValueError, TypeError) as exc:
                        raise AssertionError({'path': name, 'pointer': pointer, 'part': part}) from exc
        feature, cohort = entry['feature'], entry['cohort']
        counts[feature][cohort] += 1
        paths[feature][cohort].add(name)
        tasks[feature][cohort].add(entry['task'])
        values[feature][str(entry['value'])] += 1
    assert set(counts) == set(stats['features'])
    for feature, row in stats['features'].items():
        assert row['counts'] == {c: counts[feature][c] for c in COHORTS}, feature
        assert row['paths'] == {c: sorted(paths[feature][c]) for c in COHORTS}, feature
        assert row['tasks'] == {c: sorted(tasks[feature][c]) for c in COHORTS}, feature
        assert row['values'] == dict(values[feature]), feature
    checks['resolvable_evidence_events'] = len(events)
    checks['feature_aggregates'] = len(counts)

    # Recount unique prompt IDs and request IDs directly from original logs.
    original_files = sorted(path for base in ('jobs', 'runs') for path in (ROOT / base).rglob('*')
                            if path.is_file() and not path.is_symlink())
    assert len(original_files) == stats['files']
    assert sum(path.suffix == '.json' for path in original_files) == stats['json_files']
    prompts, completed, requests, responses = set(), set(), set(), set()
    repair_outputs = defaultdict(set)
    for path in original_files:
        if path.name == 'state.json' and not any(part in ('cache', 'portrait_cache', 'edits') for part in path.parts):
            data = json.loads(path.read_text())
            for attempt in data.get('attempts', []):
                if attempt.get('prompt_id') and not attempt.get('inherited'):
                    prompts.add(attempt['prompt_id'])
                    if attempt.get('status') == 'completed':
                        completed.add(attempt['prompt_id'])
            for key, repair in data.get('repairs', {}).items():
                own = [a for a in data.get('attempts', []) if a.get('repair_key') == key and not a.get('inherited')]
                if repair.get('output') and any(a.get('status') == 'completed' for a in own):
                    stage = repair.get('stages')
                    identity = stage[0] if stage else (str(path.relative_to(ROOT)), key)
                    primitive = repair.get('request', {}).get('primitive', 'unknown')
                    repair_outputs[primitive].add(identity)
        elif path.suffix == '.jsonl':
            for index, line in enumerate(path.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                entry = json.loads(line)
                identity = entry.get('call_id') or (str(path.relative_to(ROOT)), index)
                if entry.get('event') == 'request':
                    requests.add(identity)
                elif entry.get('event') == 'response':
                    responses.add(identity)
    assert len(prompts) == sum(counts['submitted_attempt'].values())
    assert len(completed) == sum(counts['completed_attempt'].values())
    assert len(requests) == stats['request_ids'] == sum(counts['review_request'].values())
    assert len(requests - responses) == stats['requests_without_response']
    assert len(responses - requests) == stats['responses_without_request']
    for primitive, records in repair_outputs.items():
        assert len(records) == sum(counts['repair_output_' + primitive].values()), primitive
    checks['independent_unique_prompt_ids'] = len(prompts)
    checks['independent_completed_prompt_ids'] = len(completed)
    checks['independent_unique_request_ids'] = len(requests)
    checks['independent_repair_outputs'] = {p: len(rows) for p, rows in sorted(repair_outputs.items())}

    for row in evidence:
        path = ROOT / row['path']
        assert path.is_file() == row['is_file'], row
        if row['is_file']:
            assert digest(path) == row['sha256'], row
    checks['validation_evidence_paths'] = len(evidence)
    doc = (OUT / 'AUDIT.md').read_text()
    table_files = re.findall(r'^\| \[src/storycg/([^\]]+)\]', doc, flags=re.M)
    assert len(table_files) == len(set(table_files)) == len(sources)
    assert set(table_files) == {path.name for path in sources}
    assert 'reports/AUD-01.md' not in doc or (ROOT / 'reports/AUD-01.md').exists()
    with (OUT / 'modules.tsv').open() as stream:
        assert len(list(csv.DictReader(stream, delimiter='\t'))) == len(sources)
    checks['audit_table_rows'] = len(table_files)
    result = {'status': 'PASS', 'method': 'Stdlib, original-file recount + hashes + event locations; no production imports/API execution.',
              'checks': checks}
    (OUT / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
