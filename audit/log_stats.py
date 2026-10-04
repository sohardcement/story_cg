#!/usr/bin/env python3
"""Scan saved evidence only, with no storycg import, model call or image write.

Counts are evidence observations, not inferred Python call counts. JSON pointers
and log line numbers are saved so every number can be traced to original data.
"""
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audit'
COHORTS = ['jobs_cli', 'jobs_development', 'runs_development']
events = []
seen = set()
inputs = {}
errors = []
excluded = Counter()
documents = {}


def rel(path):
    return str(path.relative_to(ROOT))


def cohort(path):
    parts = path.relative_to(ROOT).parts
    if parts[0] == 'jobs':
        return 'jobs_cli' if parts[1].startswith('cg-') else 'jobs_development'
    return 'runs_development'


def suite(path):
    return '/'.join(path.relative_to(ROOT).parts[:2])


def load(path):
    key = rel(path)
    if key in documents:
        return documents[key]
    try:
        raw = path.read_bytes()
        inputs[key] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        value = json.loads(raw)
        documents[key] = value
        return value
    except (ValueError, OSError) as exc:
        errors.append({'path': key, 'error': str(exc)})
        return None


def add(feature, path, pointer='', *, key=None, task=None, value=None, note=''):
    dedup = (feature, key if key is not None else (rel(path), pointer))
    if dedup in seen:
        excluded['duplicate_feature_records'] += 1
        return
    seen.add(dedup)
    events.append({'feature': feature, 'cohort': cohort(path), 'suite': suite(path),
                   'task': task or rel(path.parent), 'path': rel(path), 'pointer': pointer,
                   'value': value, 'note': note})


def ordinary(path):
    parts = path.relative_to(ROOT).parts
    return 'portrait_cache' not in parts and 'edits' not in parts and 'cache' not in parts


def branch_features(entry):
    purpose = entry.get('purpose', '')
    schema = entry.get('request', {}).get('response_format', {}).get('json_schema', {}).get('schema', {})
    props = schema.get('properties', {})
    fs = ['review_request']
    if purpose.startswith('cycle_llm_'):
        fs.append('diagnosis_request')
    else:
        fs.append('vision_request')
    if purpose.startswith('tier_') or purpose.startswith('l2_test'):
        fs.append('tier_comparison_request')
    if purpose.startswith('portrait_audit_'):
        fs.append('portrait_audit_request')
    if purpose == 'narrative_blind':
        fs.append('blind_review_request')
    if purpose == 'narrative_compare':
        fs.append('narrative_compare_request')
    if purpose.startswith('v3_') or purpose.endswith('_v3'):
        fs.append('supplement_review_request')
    if purpose.startswith('single_') or purpose.endswith('_single') or purpose == 'repair_single':
        fs.append('single_review_request')
    if purpose.startswith('consistency_') or purpose.endswith(('_identity_outfit', '_identity', '_consistency')) or '_neighbor' in purpose:
        fs.append('consistency_review_request')
    if any(k.startswith('prop_') for k in props):
        fs.append('prop_audit_request')
    if '_bbox' in purpose:
        fs.append('legacy_vlm_bbox_request')
    if 'neighbor' in purpose:
        fs.append('neighbor_review_request')
    return fs


def main():
    files = sorted(p for base in ['jobs', 'runs'] for p in (ROOT / base).rglob('*')
                   if p.is_file() and not p.is_symlink())
    inventory = Counter(p.suffix for p in files)
    json_files = [p for p in files if p.suffix == '.json']
    # Full JSON inventory, including caches, so omissions and parse failures are explicit.
    for p in json_files:
        load(p)

    # Prefer the completed record of a prompt_id over an old submitted snapshot.
    # Original jobs sort before copied runs; the completed state of an interrupted
    # development task can live in its follow-up work-order directory.
    canonical_attempts = {}
    attempt_copies = defaultdict(list)
    for p in json_files:
        d = load(p)
        if p.name != 'state.json' or not ordinary(p) or not isinstance(d, dict):
            continue
        for i, a in enumerate(d.get('attempts', [])):
            if a.get('inherited') or not a.get('prompt_id'):
                continue
            pid = a['prompt_id']
            attempt_copies[pid].append((p, i, a.get('status')))
            old = canonical_attempts.get(pid)
            if old is None or (a.get('status') == 'completed' and old[2] != 'completed'):
                canonical_attempts[pid] = (p, i, a.get('status'))
    for pid, copies in attempt_copies.items():
        if len({row[2] for row in copies}) > 1:
            p, i, status = canonical_attempts[pid]
            add('saved_prompt_status_transition', p, f'/attempts/{i}', key=pid,
                value=[{'path': rel(q), 'status': s} for q, _, s in copies],
                note='Same prompt_id has submitted and completed saved states; no exact reconcile call count inferred.')

    task_rows = []
    for p in json_files:
        if not ordinary(p):
            excluded['cache_or_archive_json'] += 1
            continue
        d = load(p)
        if d is None:
            continue
        root = p.parent
        if p.name == 'job.json' and isinstance(d, dict):
            add('job_record', p)
            state = load(root / 'state.json') if (root / 'state.json').exists() else {}
            agent = load(root / 'agent.json') if (root / 'agent.json').exists() else {}
            identity = load(root / 'identity.json') if (root / 'identity.json').exists() else {}
            task_rows.append({'task': rel(root), 'cohort': cohort(p),
                              'phase': (agent or {}).get('phase'), 'tier': (identity or {}).get('identity_tier'),
                              'copied_repair_source': (state or {}).get('repair_source'),
                              'has_cycle': (root / 'cycle.json').exists(),
                              'has_review': (root / 'review.json').exists(),
                              'has_sheet': (root / 'contact_sheet.jpg').exists()})
            if (root / 'contact_sheet.jpg').exists():
                add('contact_sheet_task', p, note='Existing file only; no rendering performed.')
            if 'generation_config' in d:
                add('generation_config_record', p, '/generation_config')
        elif p.name == 'agent.json' and isinstance(d, dict):
            add('agent_record', p)
            intake = d.get('intake', {})
            if intake:
                add('intake_record', p, '/intake')
            for k in ['possession_swap', 'prop_dependent']:
                if k in intake:
                    add('intake_' + k + '_record', p, '/intake/' + k, value=intake[k])
                    if intake[k] is True:
                        add('intake_' + k + '_true', p, '/intake/' + k)
            if d.get('storyboard'):
                add('storyboard_presentation_record', p, '/storyboard')
            if d.get('approved') is True:
                add('approved_record', p, '/approved')
            if d.get('storyboard_first') is True:
                add('storyboard_first_record', p, '/storyboard_first')
        elif p.name in ('plan_log.json', 'agent_llm_log.json') and isinstance(d, dict):
            for i, call in enumerate(d.get('calls', [])):
                prefix = f'/calls/{i}'
                feature = 'planning_call' if p.name == 'plan_log.json' else 'agent_llm_call'
                add(feature, p, prefix, value=call.get('status'))
                if call.get('rule_id'):
                    add('planner_rule_rejection', p, prefix, value=call['rule_id'])
                if call.get('tag_validation'):
                    add('tag_validation_call_record', p, prefix + '/tag_validation')
                    for j, tag in enumerate(call['tag_validation']):
                        add('tag_validation_item', p, prefix + f'/tag_validation/{j}', value=tag.get('action'))
                if call.get('planning_contract') == 'single_moment_compiled_v2':
                    add('compiled_planning_call', p, prefix)
        elif p.name == 'identity.json' and isinstance(d, dict):
            state = load(root / 'state.json') if (root / 'state.json').exists() else {}
            copied = bool((state or {}).get('repair_source'))
            add('identity_record', p, value=d.get('identity_tier'), note='copied repair identity' if copied else '')
            if not copied:
                add('identity_origin_record', p, value=d.get('identity_tier'))
                tier = d.get('identity_tier')
                if tier in ('L1', 'L2', 'L3', 'L4'):
                    add('identity_final_' + tier, p, '/identity_tier', note='Final saved classification; not count of tier transitions.')
                for k in ['appearance_candidates', 'identity_rules', 'default_outfit_selection', 'alternate_outfits']:
                    if d.get(k):
                        add(k + '_record', p, '/' + k)
                if d.get('tier_evidence'):
                    add('tier_evidence_record', p, '/tier_evidence')
                if d.get('tier_evidence', {}).get('portrait_cache_source'):
                    add('l3_reuse_record', p, '/tier_evidence/portrait_cache_source')
            if d.get('status') == 'needs_clarification':
                add('identity_ambiguity_record', p, '/status')
        elif p.name == 'portrait.json' and isinstance(d, dict):
            state = load(root / 'state.json') if (root / 'state.json').exists() else {}
            copied = bool((state or {}).get('repair_source'))
            add('portrait_record', p, note='copied repair portrait' if copied else '')
            if not copied:
                add('portrait_origin_record', p)
            if d.get('cache_hit') is True:
                add('portrait_cache_marker', p, '/cache_hit', note='copied repair portrait' if copied else '')
                if not copied:
                    add('portrait_cache_hit_task', p, '/cache_hit', note='cache_hit=true at destination; no inference of repeat hit calls.')
            if not copied and d.get('gate_policy') == 'D7':
                add('d7_portrait_record', p, '/gate_policy')
            if not copied and d.get('gate_policy') == 'L3 reliable comparison':
                add('l3_portrait_record', p, '/gate_policy')
        elif p.name == 'state.json' and isinstance(d, dict):
            add('state_record', p)
            for i, a in enumerate(d.get('attempts', [])):
                if a.get('inherited'):
                    excluded['inherited_attempt'] += 1
                    continue
                prefix = f'/attempts/{i}'
                pid = a.get('prompt_id')
                if pid and canonical_attempts[pid][:2] != (p, i):
                    excluded['duplicate_prompt_id_snapshot'] += 1
                    continue
                key = pid or (rel(p), a.get('shot_id'), a.get('attempt_number'))
                add('generation_attempt', p, prefix, key=key, value=a.get('status'))
                if pid:
                    add('submitted_attempt', p, prefix, key=pid, value=a.get('status'))
                    if a.get('shot_id', '').startswith('shot'):
                        add('story_shot_attempt', p, prefix, key=pid)
                if a.get('status') == 'completed':
                    add('completed_attempt', p, prefix, key=key)
                ref = a.get('reference') or {}
                if ref.get('mechanism'):
                    add('reference_' + ref['mechanism'], p, prefix + '/reference', key=key)
                # LoRA/IPAdapter can also be nested in img2img references.
                if 'IPAdapterAdvanced' in {n.get('class_type') for n in a.get('workflow', {}).values()}:
                    add('ipadapter_workflow_attempt', p, prefix + '/workflow', key=key)
            for k, rec in d.get('repairs', {}).items():
                primitive = rec.get('request', {}).get('primitive', 'unknown')
                own = [a for a in d.get('attempts', []) if a.get('repair_key') == k and not a.get('inherited')]
                # A recovered relayout may have a final stage only in the new
                # directory. Its first persisted stage prefix stays stable.
                key = rec.get('stages', [None])[0] if rec.get('stages') else (rel(p), k)
                add('repair_record_' + primitive, p, '/repairs/' + k, key=key, value=rec.get('status'))
                if any(a.get('prompt_id') for a in own):
                    add('repair_submitted_' + primitive, p, '/repairs/' + k, key=key)
                if rec.get('output') and any(a.get('status') == 'completed' for a in own):
                    add('repair_output_' + primitive, p, '/repairs/' + k, key=key)
                if rec.get('boundary_check'):
                    add('repair_boundary_check', p, '/repairs/' + k + '/boundary_check', key=key, value=rec['boundary_check'].get('passed'))
                operation = rec.get('request', {}).get('edit', {}).get('operation')
                if operation and own:
                    add('instruction_operation_' + operation, p, '/repairs/' + k + '/request/edit', key=key)
        elif p.name == 'cycle.json' and isinstance(d, dict):
            add('cycle_record', p, value=d.get('phase'))
            if d.get('repair_scope'):
                add('targeted_repair_record', p, '/repair_scope')
            for n, slot in d.get('shots', {}).items():
                for i, h in enumerate(slot.get('history', [])):
                    primitive = h.get('decision', {}).get('primitive', 'unknown')
                    prefix = f'/shots/{n}/history/{i}'
                    add('cycle_decision_' + primitive, p, prefix, value=h.get('status'))
                    if h.get('result', {}).get('improved') is True:
                        add('cycle_accepted_' + primitive, p, prefix + '/result')
                    if h.get('decision', {}).get('problem') == 'props':
                        add('cycle_prop_decision', p, prefix + '/decision')
                    if h.get('narrative'):
                        add('cycle_narrative_decision', p, prefix)
                    if h.get('result', {}).get('improvement_basis') == 'reliable_severity_vector':
                        add('d29_comparison_record', p, prefix + '/result')
        elif p.name == 'storyboard.json' and isinstance(d, dict):
            state = load(root / 'state.json') if (root / 'state.json').exists() else {}
            if (state or {}).get('repair_source'):
                excluded['copied_storyboard'] += 1
                continue
            add('storyboard_record', p)
            if d.get('schema_version') == 2:
                add('shot_contract_v2_board', p, '/schema_version')
            if 'possession_swap' in d:
                add('board_possession_swap_record', p, '/possession_swap', value=d['possession_swap'])
                if d['possession_swap'] is True:
                    add('board_possession_swap_true', p, '/possession_swap')
            if d.get('scope_version') == 1:
                add('scope_board_record', p, '/scope_version')
            for i, prop in enumerate(d.get('props', [])):
                add('registered_prop', p, f'/props/{i}')
            for i, shot in enumerate(d.get('shots', [])):
                for j, ref in enumerate(shot.get('shot', {}).get('key_props', [])):
                    add('prop_binding', p, f'/shots/{i}/shot/key_props/{j}')
                    if ref.get('detached') is True:
                        add('detached_prop_binding', p, f'/shots/{i}/shot/key_props/{j}/detached')
        elif p.name == 'manifest.json' and isinstance(d, dict) and (root / 'job.json').exists():
            add('story_manifest_record', p)
            if any('prompt_parts' in x for x in d.get('images', [])):
                add('prompt_parts_manifest_record', p, '/images')
        elif p.name == 'inventory.json' and isinstance(d, dict) and 'tier' in p.parts:
            add('tier_inventory_record', p)
            if 'metadata_status' in d:
                add('lora_metadata_inventory_record', p, '/metadata_status')
                for name, record in d['metadata_status'].items():
                    if 'cache_hit' in record:
                        add('lora_metadata_cache_marker', p, '/metadata_status/' + name, value=record['cache_hit'])
                        if record['cache_hit'] is True:
                            add('lora_metadata_cache_hit_marker', p, '/metadata_status/' + name)
        elif p.name == 'reference_uploads.json' and isinstance(d, dict):
            add('reference_upload_record', p)
            for key in d:
                add('uploaded_reference_hash', p, '/' + key)
        elif p.name == 'review_gate.json' and isinstance(d, dict):
            add('group_gate_record', p, value=d.get('conclusion'))
            if d.get('policy') == 'D23/D24 v1 person scope':
                add('person_scope_gate_record', p, '/policy')
        elif p.name == 'tier_gate.json' or p.name.endswith('.gate.json'):
            if isinstance(d, dict):
                add('tier_gate_artifact', p)
        elif p.name == 'portrait_gate_original.json':
            add('d7_recovery_artifact', p)
        elif p.name == 'review.json' and isinstance(d, dict):
            add('group_review_record', p, value=d.get('conclusion'))
            if d.get('blind_thumbnails'):
                add('blind_thumbnails_record', p, '/blind_thumbnails')
            if d.get('detection_items'):
                add('group_detection_record', p, '/detection_items')
            if d.get('supplementary') or any('v3' in str(k) for k in d):
                add('group_supplement_record', p)

    log_event_counts = Counter()
    request_ids = set()
    responses = {}
    for p in [p for p in files if p.suffix == '.jsonl']:
        raw = p.read_bytes()
        inputs[rel(p)] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        for line_no, line in enumerate(raw.decode().splitlines(), 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError as exc:
                errors.append({'path': rel(p), 'line': line_no, 'error': str(exc)})
                continue
            log_event_counts[(cohort(p), d.get('event', 'unknown'))] += 1
            if d.get('event') == 'request':
                call_id = d.get('call_id') or (rel(p), line_no)
                if call_id in request_ids:
                    excluded['duplicate_request_call_id'] += 1
                    continue
                request_ids.add(call_id)
                for feature in branch_features(d):
                    add(feature, p, f'line:{line_no}', key=call_id, value=d.get('purpose'))
            elif d.get('event') == 'response':
                responses[d.get('call_id', (rel(p), line_no))] = d
            elif d.get('event') == 'blind_thumbnail':
                add('blind_thumbnail_log', p, f'line:{line_no}')

    text_logs = []
    for p in [p for p in files if p.suffix in ('.log', '.txt')]:
        raw = p.read_bytes()
        inputs[rel(p)] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        lines = raw.decode(errors='replace').splitlines()
        phases = Counter()
        for line_no, line in enumerate(lines, 1):
            match = re.fullmatch(r'storycg ([\w-]+): ([\w_]+)', line.strip())
            if match:
                phase = match.group(2)
                phases[phase] += 1
                add('worker_phase_log', p, f'line:{line_no}', value=phase)
        text_logs.append({'path': rel(p), 'bytes': len(raw), 'lines': len(lines), 'phases': dict(phases)})
    (OUT / 'text_logs.json').write_text(json.dumps(text_logs, ensure_ascii=False, indent=2) + '\n')

    # Observations inside immutable review artifacts. Same image+item observation
    # is counted once, despite duplication in cycle, manifest and final reports.
    for p in json_files:
        if not ordinary(p) or p.name in ('state.json', 'manifest.json'):
            continue
        d = load(p)
        if not isinstance(d, (dict, list)) or ('cycle_artifacts' not in p.parts and p.name not in ['review.json', 'portrait_attempts.json']):
            continue
        def walk(value, pointer=''):
            if isinstance(value, dict):
                for k, child in value.items():
                    if k in ('eye_color_sampled', 'accessory_color_sampled', 'protagonist_count',
                             'extra_protagonist', 'outfit_detail_vs_portrait', 'accessories_zoom', 'hair_ornaments_zoom') and isinstance(child, dict) and child.get('status') in ('pass','fail','minor','not_visible','ok','mismatch','duplicate','none_visible','uncertain','not_applicable'):
                        key = hashlib.sha256(json.dumps(child, sort_keys=True).encode()).hexdigest()
                        add('observation_' + k, p, pointer + '/' + k, key=key, value=child.get('status'),
                            note='Unique serialized observation, not audit invocation count.')
                    walk(child, pointer + '/' + k)
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    walk(child, pointer + '/' + str(i))
        walk(d)

    counts = defaultdict(Counter)
    paths = defaultdict(lambda: defaultdict(set))
    tasks = defaultdict(lambda: defaultdict(set))
    values = defaultdict(Counter)
    for e in events:
        counts[e['feature']][e['cohort']] += 1
        paths[e['feature']][e['cohort']].add(e['path'])
        tasks[e['feature']][e['cohort']].add(e['task'])
        values[e['feature']][str(e['value'])] += 1
    features = {k: {'counts': {c: counts[k][c] for c in COHORTS},
                    'tasks': {c: sorted(tasks[k][c]) for c in COHORTS},
                    'paths': {c: sorted(paths[k][c]) for c in COHORTS},
                    'values': dict(values[k])} for k in sorted(counts)}
    result = {'cohorts': {'jobs_cli': 'jobs/cg-* (CLI facade; contains development/Hermes tests, not a claim of production traffic)',
                           'jobs_development': 'jobs/p* named P-work-order regression jobs',
                           'runs_development': 'runs/*, excluding cache storage and identity archives from functional counts'},
              'files': len(files), 'json_files': len(json_files), 'inputs': len(inputs),
              'file_types': dict(inventory), 'parse_errors': errors, 'excluded': dict(excluded),
              'request_ids': len(request_ids), 'responses_without_request': len(set(responses) - request_ids),
              'requests_without_response': len(request_ids - set(responses)),
              'log_events': {c: {event: count for (group, event), count in log_event_counts.items() if group == c} for c in COHORTS},
              'features': features, 'task_records': task_rows}
    (OUT / 'log_stats.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (OUT / 'input_files.json').write_text(json.dumps(inputs, ensure_ascii=False, indent=2) + '\n')
    with (OUT / 'events.jsonl').open('w') as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    with (OUT / 'feature_counts.tsv').open('w', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(['feature', *COHORTS, 'total', 'value_counts'])
        for k, f in features.items():
            row = [f['counts'][c] for c in COHORTS]
            writer.writerow([k, *row, sum(row), json.dumps(f['values'], ensure_ascii=False)])
    print(json.dumps({'files': len(files), 'parsed_json_files': len(json_files), 'events': len(events),
                      'parse_errors': len(errors), 'unique_request_ids': len(request_ids),
                      'features': len(features), 'tasks': len(task_rows)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
