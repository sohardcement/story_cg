"""V2-20 check: every [node_id, output] reference points at an existing node; list LoRA nodes."""
import json
import sys

LORA = {"LoraLoader", "LoraLoaderModelOnly"}


def check(path):
    cases = json.loads(open(path, encoding="utf-8").read())
    rows = []
    for name, graph in cases.items():
        dangling = []
        for node_id, node in graph.items():
            for field, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and not isinstance(value[1], str):
                    if value[0] not in graph:
                        dangling.append(f"{node_id}.{field}->{value[0]}")
        loras = sorted(k for k, n in graph.items() if n["class_type"] in LORA)
        rows.append((name, loras, dangling))
    return rows


if __name__ == "__main__":
    for name, loras, dangling in check(sys.argv[1]):
        print(f"{name:24s} lora_nodes={loras} dangling={dangling or 'none'}")
