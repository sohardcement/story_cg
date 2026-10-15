"""Shot vocabulary (TASK-v2 §2): probe-graded framing/angle/action combos.

Grades come from config/shot_vocab.json (generated from the capability probe;
see TASK-v2 §2.3). This module only enforces and reports those grades:
- hit rule: framing + camera_angle + action tag all equal to an entry's;
  scene differences never affect the grade; expression only picks among entries
  that share framing, angle and action (V2-12, D57: see classify_shots);
  same action with a different framing/angle is off-table;
- each entry has one grade (V2-43b: the L1 column and the tier argument are gone);
- off-table actions are 'untested' (V2-62, D96; before that they counted as
  sometimes, D32) and take no quota; at most one sometimes shot per storyboard;
  unusable is rejected with the registered substitute;
- off-table results observed at runtime are appended to an observation log,
  marked not human-judged; grades change only through user decisions.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from storycg.story_rules import StoryRuleViolation

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_VOCAB = PROJECT / "config" / "shot_vocab.json"
DEFAULT_OBSERVATION_LOG = PROJECT / "calibration" / "shot_vocab_observations.jsonl"
GRADE_ORDER = {"stable": 0, "sometimes": 1, "unusable": 2}
V2_SCOPE = 2


def vocab_path(config=None):
    value = (config or {}).get("shot_vocab", {}).get("path")
    if not value:
        return DEFAULT_VOCAB
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT / path


def observation_log_path(config=None):
    value = (config or {}).get("shot_vocab", {}).get("observation_log")
    if not value:
        return DEFAULT_OBSERVATION_LOG
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT / path


def load_vocab(config=None):
    return json.loads(vocab_path(config).read_text(encoding="utf-8"))


def grade_of(item):
    """Grade of one vocab entry: stable, sometimes or unusable (V2-43b: one column, no tier)."""
    return item["grade"]


def replacement_for(item):
    """Substitute record for an unusable entry, or None when none is registered."""
    return item.get("replacement")


def substitute_text(item):
    row = replacement_for(item)
    if row is None:
        return "无登记替代"
    if row.get("substitute_id") is None:
        return f"无 id；改写成“{row['rewrite']}”" + (f"（{row['note']}）" if row.get("note") else "")
    return f"{row['substitute_id']}" + (f"（{row['note']}）" if row.get("note") else "")


def classify_shots(board, vocab=None):
    """Per-shot vocabulary classification; never raises on grades.

    A shot hits an entry when framing, camera_angle and the canonical action
    tag all agree. Expression differences are recorded, not penalized. Shots
    whose action matches an entry with another framing/angle list those ids
    separately so the observation log can see the near misses.
    """
    vocab = vocab or load_vocab()
    by_action = {}
    for item in vocab["items"]:
        for action in item["action_tags"]:
            by_action.setdefault(action, []).append(item)
    shots = []
    for index, entry in enumerate(board.get("shots", [])):
        shot = entry["shot"]
        action = str(shot["pose"]).strip().lower()
        action = "_".join(action.split())
        hits = [item for item in by_action.get(action, [])
                if item["framing"] == shot["framing"] and item["camera_angle"] == shot["camera_angle"]]
        near = [item["id"] for item in by_action.get(action, [])
                if item["framing"] != shot["framing"] or item["camera_angle"] != shot["camera_angle"]]
        if hits:
            # V2-12 (D57): when the shot's expression belongs to only some of the matching entries,
            # those entries decide the grade; when none or all of them list it, the conservative max applies.
            expression_matches = [shot["expression"] in item["expression_tags"] for item in hits]
            scoped = [item for item, matched in zip(hits, expression_matches) if matched]
            scope = scoped if scoped else hits
            grade = max((grade_of(item) for item in scope), key=lambda g: GRADE_ORDER[g])
        else:
            grade, expression_matches = "untested", []  # V2-62 (D96): off-table takes no 'sometimes' quota
        shots.append({"shot": entry.get("number", index + 1), "action": action,
                      "framing": shot["framing"], "camera_angle": shot["camera_angle"],
                      "expression": shot["expression"], "vocab_ids": [item["id"] for item in hits],
                      "off_table": not hits, "grade": grade,
                      "expression_matches": expression_matches,
                      "action_matches_other_framing": near})
    return shots


def validate_shot_vocab(board, config=None, vocab=None):
    """Enforce §2.2 vocabulary rules on a v2 storyboard; return classifications.

    Legacy (scope_version<2) boards are out of scope and return no checks.
    Raises StoryRuleViolation with the offending shot named so planner retries
    carry the exact violation, e.g. 第 3 镜：动作 running 为不可用，替代为 18.
    """
    if board.get("scope_version") != V2_SCOPE:
        return []
    vocab = vocab or load_vocab(config)
    by_id = {item["id"]: item for item in vocab["items"]}
    shots = classify_shots(board, vocab=vocab)
    for classified in shots:
        if classified["grade"] != "unusable":
            continue
        item = by_id[classified["vocab_ids"][0]]
        problem = (f"第 {classified['shot']} 镜：动作 {classified['action']} 为不可用，"
                   f"替代为 {substitute_text(item)}")
        raise StoryRuleViolation(
            "shot_vocab_unusable",
            [f"$.shots[{classified['shot'] - 1}].shot.pose",
             f"$.shots[{classified['shot'] - 1}].shot.framing",
             f"$.shots[{classified['shot'] - 1}].shot.camera_angle"],
            problem,
            "改用替代组合或稳定组合；替代时在 story_note 里用一句中文记录改写")
    sometimes = [c for c in shots if c["grade"] == "sometimes"]
    if len(sometimes) > 1:
        parts = [f"第 {c['shot']} 镜：动作 {c['action']} 为有时{'（表外）' if c['off_table'] else ''}"
                 for c in sometimes]
        problem = "；".join(parts) + "；每组最多 1 镜有时（表外动作不算）"
        fix = "换用稳定组合；或保留 1 镜有时，其余改为稳定组合"
        raise StoryRuleViolation(
            "shot_vocab_sometimes_limit",
            [f"$.shots[{c['shot'] - 1}].shot.pose" for c in sometimes],
            problem,
            fix)
    return shots


def _combo(item):
    return f"{item['id']} {item['framing']}/{item['camera_angle']} {item['action_tags'][0]}"


def prompt_section(config=None, vocab=None):
    """V2-63 (D96, TASK-v2 §12 item 1): the vocabulary lines of beats-guide, short and in Chinese.

    One line per grade, each combo written as `id framing/angle pose`; unusable combos name their substitute.
    """
    items = (vocab or load_vocab(config))["items"]
    stable = [_combo(item) for item in items if grade_of(item) == "stable"]
    sometimes = [_combo(item) for item in items if grade_of(item) == "sometimes"]
    unusable = [f"{_combo(item)} → {substitute_text(item)}" for item in items if grade_of(item) == "unusable"]
    lines = ["一个镜头按“景别/角度 pose”对表，表情可以不同。"]
    if stable:
        lines.append("- 稳定，优先用：" + "；".join(stable))
    if sometimes:
        lines.append("- 有时，每组最多 1 镜：" + "；".join(sometimes))
    if unusable:
        lines.append("- 不可用，不要用（改用替代，并在 story_note 里用一句中文记下）：" + "；".join(unusable))
    lines.append("- 表里没有的组合可以用，记为未测，不占名额。")
    return "\n".join(lines)


def _review_conclusion(root, number):
    cycle_path = root / "cycle.json"
    if not cycle_path.exists():
        return {"available": False, "note": "修复循环未运行，无运行时审核结论"}
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    slot = (cycle.get("shots") or {}).get(str(number))
    if not slot:
        return {"available": False, "note": "该镜无运行时审核记录"}
    audit = slot.get("best_audit", {})
    conclusion = slot.get("final_conclusion") or audit.get("conclusion")
    layers = {key: (audit.get(key) or {}).get("status") if isinstance(audit.get(key), dict) else None
              for key in ("single", "consistency", "detection")}
    return {"available": True, "final_conclusion": conclusion, **layers}


def _evidence_image(root, number):
    cycle_path = root / "cycle.json"
    if cycle_path.exists():
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        slot = (cycle.get("shots") or {}).get(str(number)) or {}
        image = (slot.get("best_audit") or {}).get("image", {})
        if image.get("path"):
            return image["path"]
    state_path = root / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        shot_id = f"shot{number:02d}"
        completed = [a for a in state.get("attempts", [])
                     if a.get("shot_id") == shot_id and a.get("status") == "completed"]
        if completed:
            return completed[-1]["image"]["path"]
    return None


def record_observations(job_dir, config=None, *, vocab=None, now=None):
    """Append one observation record per off-table shot after a task finishes.

    Records are marked not human-judged; they never change a grade by
    themselves. Runtime review conclusion and evidence image come from the
    finished cycle or state.
    """
    root = Path(job_dir)
    board_path = root / "storyboard.json"
    if not board_path.exists():
        return []
    board = json.loads(board_path.read_text(encoding="utf-8"))
    if board.get("scope_version") != V2_SCOPE:
        return []
    vocab = vocab or load_vocab(config)
    recorded = []
    for classified in classify_shots(board, vocab=vocab):
        if not classified["off_table"]:
            continue
        recorded.append({
            "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
            "job_id": root.name,
            "shot": classified["shot"],
            "action_key": classified["action"],
            "framing": classified["framing"],
            "camera_angle": classified["camera_angle"],
            "expression": classified["expression"],
            "grade": classified["grade"],
            "off_table": True,
            "action_matches_other_framing": classified["action_matches_other_framing"],
            "runtime_review": _review_conclusion(root, classified["shot"]),
            "evidence_image": _evidence_image(root, classified["shot"]),
            "human_judged": False,
            "note": "未经人工判定；不自动改分档",
        })
    if not recorded:
        return []
    log_path = observation_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        for record in recorded:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return recorded
