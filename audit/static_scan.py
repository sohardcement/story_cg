#!/usr/bin/env python3
"""Read-only AST audit. Never imports storycg or executes its entry points."""
import argparse
import ast
import csv
import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audit'
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def write_json(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def snapshot():
    directories = ['src', 'skill', 'config', 'tests', 'tools', 'jobs', 'runs', 'orders']
    paths = [ROOT / x for x in ['TASK.md', 'TASK-CAP.md', 'AGENTS.md', 'SUPERVISOR.md', 'PROGRESS.md']]
    for directory in directories:
        paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and not p.is_symlink())
    return {str(p.relative_to(ROOT)): {'size': p.stat().st_size,
                                      'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(set(paths)) if p.is_file()}


def scan():
    modules = {}
    defs = {}
    scopes = {}
    parents = {}
    classes = {}
    imports = defaultdict(list)
    module_edges = []

    def collect(body, module, scope):
        for node in body:
            if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
                name = scope + '.' + node.name
                scopes[scope][node.name] = name
                scopes[name] = {}
                parents[name] = scope
                defs[name] = {'module': module, 'file': modules[module]['file'],
                              'line': node.lineno, 'end': node.end_lineno,
                              'kind': 'class' if isinstance(node, ast.ClassDef) else 'function',
                              'node': node, 'scope': name}
                if isinstance(node, ast.ClassDef):
                    classes[name] = [ast.unparse(x) for x in node.bases]
                collect(node.body, module, name)
            elif isinstance(node, ast.ImportFrom):
                imports[scope].append(node)
            elif isinstance(node, ast.Import):
                imports[scope].append(node)
            else:
                # Imports/defs under if/try/with/loops retain the Python scope.
                for field in ('body', 'orelse', 'finalbody'):
                    nested = getattr(node, field, None)
                    if isinstance(nested, list):
                        collect(nested, module, scope)
                for handler in getattr(node, 'handlers', []):
                    collect(handler.body, module, scope)

    for path in sorted((ROOT / 'src/storycg').glob('*.py')):
        module = 'storycg.' + path.stem if path.stem != '__init__' else 'storycg'
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        modules[module] = {'file': str(path.relative_to(ROOT)), 'lines': len(text.splitlines()),
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'tree': tree}
        scopes[module] = {}
        collect(tree.body, module, module)

    for _ in range(4):
        for scope, nodes in imports.items():
            for node in nodes:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        scopes[scope][alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
                        if alias.name in modules:
                            module_edges.append((next((m for m in modules if scope == m or scope.startswith(m + '.')), scope), alias.name))
                else:
                    module = node.module or ''
                    if module not in modules:
                        continue
                    for alias in node.names:
                        if alias.name == '*':
                            for k, v in scopes[module].items():
                                if not k.startswith('_'):
                                    scopes[scope].setdefault(k, v)
                        else:
                            target = module + '.' + alias.name
                            if module == 'storycg' and target in modules:
                                pass
                            else:
                                target = scopes[module].get(alias.name, target)
                            scopes[scope][alias.asname or alias.name] = target
                            if target in modules:
                                module_edges.append((next((m for m in modules if scope == m or scope.startswith(m + '.')), scope), target))
                            else:
                                module_edges.append((next((m for m in modules if scope == m or scope.startswith(m + '.')), scope), module))

    def lookup(scope, name):
        while scope:
            if name in scopes.get(scope, {}):
                return scopes[scope][name]
            scope = parents.get(scope)
        return None

    def target(expr, scope, types):
        if isinstance(expr, ast.Name):
            return types.get(expr.id) or lookup(scope, expr.id)
        if isinstance(expr, ast.Attribute):
            base = target(expr.value, scope, types)
            if base in modules:
                return scopes[base].get(expr.attr, base + '.' + expr.attr)
            if base in classes:
                direct = base + '.' + expr.attr
                if direct in defs:
                    return direct
                for parent in classes[base]:
                    p = lookup(parents[base], parent)
                    if p and p + '.' + expr.attr in defs:
                        return p + '.' + expr.attr
            if base:
                return base + '.' + expr.attr
        if isinstance(expr, ast.Call):
            # A function result is not its defining namespace. Only constructors
            # preserve a type for subsequent method lookup.
            resolved = target(expr.func, scope, types)
            return resolved if resolved in classes else None
        return None

    edges = []
    unresolved = []
    references = defaultdict(list)
    hints = {'client': ['storycg.comfy.ComfyClient', 'storycg.budget.GuardedComfy'],
             'vlm': ['storycg.review.VisionClient', 'storycg.budget.CycleClient'],
             'source': ['storycg.identity._CachedDanbooru'],
             'registry': ['storycg.tags.TagRegistry'],
             'budget': ['storycg.budget.Budget'], 'gate': ['storycg.budget.Budget']}

    def body_nodes(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*FUNCTIONS, ast.ClassDef)):
                # Definition/default/decorator references happen in the parent.
                for dec in getattr(child, 'decorator_list', []):
                    yield from ast.walk(dec)
                continue
            yield child
            yield from body_nodes(child)

    def add_edge(caller, callee, line, kind):
        if callee in classes:
            edges.append({'caller': caller, 'callee': callee, 'line': line, 'kind': kind})
            init = callee + '.__init__'
            if init in defs:
                edges.append({'caller': callee, 'callee': init, 'line': line, 'kind': 'constructor'})
        elif callee in defs:
            edges.append({'caller': caller, 'callee': callee, 'line': line, 'kind': kind})

    for name, meta in defs.items():
        if meta['kind'] == 'class':
            continue
        scope = name
        parent = parents[name]
        types = {'self': parent} if parent in classes else {}
        nodes = list(body_nodes(meta['node']))
        for _ in range(3):
            for n in nodes:
                if isinstance(n, (ast.Assign, ast.AnnAssign)):
                    value = n.value
                    resolved = target(value, scope, types)
                    names = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for lhs in names:
                        if isinstance(lhs, ast.Name) and resolved in classes:
                            types[lhs.id] = resolved
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            resolved = target(n.func, scope, types)
            if resolved in defs:
                add_edge(name, resolved, n.lineno, 'resolved')
            elif isinstance(n.func, ast.Attribute):
                receiver = n.func.value
                possible = []
                if isinstance(receiver, ast.Name) and receiver.id in hints:
                    for cls in hints[receiver.id]:
                        method = cls + '.' + n.func.attr
                        if method in defs:
                            possible.append(method)
                # super().__init__/call and GuardedComfy proxy forwarding.
                if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == 'super' and parent in classes:
                    for base in classes[parent]:
                        cls = lookup(parents[parent], base) or target(ast.parse(base, mode='eval').body, parents[parent], {})
                        if cls and cls + '.' + n.func.attr in defs:
                            possible.append(cls + '.' + n.func.attr)
                for method in possible:
                    add_edge(name, method, n.lineno, 'receiver_hint')
                if not possible and (resolved or '').startswith('storycg'):
                    unresolved.append({'caller': name, 'line': n.lineno, 'call': ast.unparse(n.func), 'resolved': resolved})
            for argument in [*n.args, *[kw.value for kw in n.keywords]]:
                ref = target(argument, scope, types)
                if ref in defs and not isinstance(argument, ast.Call):
                    add_edge(name, ref, n.lineno, 'callback_reference')

    # Explicit subprocess and protocol edges, verified in source; not execution evidence.
    manual = [('storycg.agent.launch', 'storycg.agent.execute', 'subprocess _worker dispatch'),
              ('storycg.budget.GuardedComfy.__init__', 'storycg.budget.GuardedComfy.__getattr__', 'proxy protocol'),
              ('storycg.budget.GuardedComfy.__init__', 'storycg.budget.Budget.check', 'self.budget boundary'),
              ('storycg.budget.CycleClient.call', 'storycg.budget.Budget.check', 'self.budget boundary'),
              ('storycg.budget.CycleClient.call', 'storycg.budget.Budget.remaining_seconds', 'self.budget boundary'),
              ('storycg.budget.GuardedComfy.wait_for_history', 'storycg.budget.Budget.remaining_seconds', 'self.budget boundary'),
              ('storycg.budget.GuardedComfy.__getattr__', 'storycg.comfy.ComfyClient.close', 'proxy delegation'),
              ('storycg.budget.GuardedComfy.wait_for_history', 'storycg.comfy.ComfyClient.wait_for_history', 'proxy delegation'),
              ('storycg.budget.GuardedComfy.submit_prompt', 'storycg.comfy.ComfyClient.submit_prompt', 'proxy delegation'),
              ('storycg.budget.CycleClient.__init__', 'storycg.review.VisionClient.__init__', 'super constructor'),
              ('storycg.budget.CycleClient.call', 'storycg.review.VisionClient.call', 'super call')]
    for caller, callee, note in manual:
        if caller in defs and callee in defs:
            edges.append({'caller': caller, 'callee': callee, 'line': defs[caller]['line'], 'kind': 'manual: ' + note})
    adjacency = defaultdict(set)
    for edge in edges:
        adjacency[edge['caller']].add(edge['callee'])

    def closure(roots):
        reached = set(roots)
        queue = deque(roots)
        while queue:
            current = queue.popleft()
            for nxt in adjacency[current] - reached:
                reached.add(nxt)
                queue.append(nxt)
        return reached

    facade = closure(['storycg.agent.main'])
    full = closure(['storycg.__main__.main'])
    # Imports and references in external callers are recorded, never executed.
    for directory in ['tests', 'tools', 'verify', 'probe', 'skill']:
        for path in sorted((ROOT / directory).rglob('*.py')):
            if path.is_symlink():
                continue
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in modules:
                    for alias in node.names:
                        if alias.name == '*':
                            aliases.update(scopes[node.module])
                        else:
                            aliases[alias.asname or alias.name] = scopes[node.module].get(alias.name, node.module + '.' + alias.name)
                        references[node.module].append({'file': str(path.relative_to(ROOT)), 'line': node.lineno, 'kind': 'module_import', 'group': directory})
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('storycg'):
                            aliases[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
            for node in ast.walk(tree):
                if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                    try:
                        parts = ast.unparse(node).split('.')
                        ref = aliases.get(parts[0], '')
                        for part in parts[1:]:
                            ref = scopes.get(ref, {}).get(part, ref + '.' + part)
                        if ref in defs:
                            references[ref].append({'file': str(path.relative_to(ROOT)), 'line': node.lineno,
                                                    'kind': 'call' if any(isinstance(p, ast.Call) and p.func is node for p in ast.walk(tree)) else 'reference', 'group': directory})
                    except (ValueError, RecursionError):
                        continue

    function_rows = []
    for name, meta in sorted(defs.items()):
        refs = sorted({(r['file'], r['line'], r['kind'], r['group']) for r in references[name]})
        incoming = sorted({e['caller'] for e in edges if e['callee'] == name})
        function_rows.append({**{k: v for k, v in meta.items() if k not in ('node', 'scope')},
                              'symbol': name, 'facade_reachable': name in facade,
                              'all_cli_reachable': name in full, 'callers': incoming,
                              'external_refs': [{'file': p, 'line': line, 'kind': kind, 'group': group} for p, line, kind, group in refs]})
    module_rows = [{**{k: v for k, v in info.items() if k != 'tree'}, 'module': name,
                    'facade_reachable_functions': sum(r['module'] == name and r['facade_reachable'] for r in function_rows),
                    'all_cli_reachable_functions': sum(r['module'] == name and r['all_cli_reachable'] for r in function_rows),
                    'external_refs': references[name]} for name, info in sorted(modules.items())]
    result = {'method': 'AST name/import resolution + explicitly labelled receiver/callback/proxy edges; branch-insensitive; not coverage.',
              'roots': {'facade': ['storycg.agent.main (make/approve/status/resume/deliver/_worker)'],
                        'all_cli': ['storycg.__main__.main (also legacy run/new/gen-one/resolve/plan/review/detect)']},
              'modules': module_rows, 'functions': function_rows, 'edges': edges,
              'unresolved_internal_calls': unresolved}
    write_json('static.json', result)
    for filename, rows, columns in [('functions.tsv', function_rows, ['symbol', 'file', 'line', 'end', 'kind', 'facade_reachable', 'all_cli_reachable', 'callers', 'external_refs']),
                                     ('call_edges.tsv', edges, ['caller', 'callee', 'line', 'kind'])]:
        with (OUT / filename).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, delimiter='\t', fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            for row in rows:
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items() if k in columns})
    module_calls = sorted({(defs[e['caller']]['module'], defs[e['callee']]['module']) for e in edges
                           if defs[e['caller']]['module'] != defs[e['callee']]['module'] and e['caller'] in full})
    dot = ['digraph storycg {', '  rankdir=LR;']
    for name in modules:
        active = any(r['module'] == name and r['all_cli_reachable'] for r in function_rows)
        dot.append(f'  "{name}" [color="{"black" if active else "gray"}"];')
    dot += [f'  "{a}" -> "{b}";' for a, b in module_calls]
    dot.append('}')
    (OUT / 'callgraph.dot').write_text('\n'.join(dot) + '\n')
    print(json.dumps({'modules': len(module_rows), 'lines': sum(x['lines'] for x in module_rows),
                      'definitions': len(function_rows), 'facade_reachable': len(facade),
                      'all_cli_reachable': len(full), 'edges': len(edges),
                      'unresolved_internal_calls': len(unresolved)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--verify-protected', action='store_true')
    args = parser.parse_args()
    if args.snapshot:
        data = snapshot()
        write_json('protected-before.json', data)
        print(json.dumps({'protected_files': len(data)}))
    elif args.verify_protected:
        before = json.loads((OUT / 'protected-before.json').read_text())
        after = snapshot()
        diff = {'changed': [p for p in before if p in after and before[p] != after[p]],
                'removed': sorted(before.keys() - after.keys()), 'added': sorted(after.keys() - before.keys())}
        write_json('protected-check.json', diff)
        print(json.dumps(diff, ensure_ascii=False))
        raise SystemExit(int(any(diff.values())))
    else:
        scan()
