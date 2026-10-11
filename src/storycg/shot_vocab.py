"""Shot vocabulary (TASK-v2 §2): probe-graded framing/angle/action combos.

Grades come from config/shot_vocab.json (generated from the capability probe;
see TASK-v2 §2.3). This module only enforces and reports those grades:
- hit rule: framing + camera_angle + action tag all equal to an entry's;
  scene differences never affect the grade; expression only picks among entries
  that share framing, angle and action (V2-12, D57: see classify_shots);
  same action with a different framing/angle is off-table;
- tier lookup: L1/L2 read their own grade; unknown/missing data take the more
  conservative of the two (stable > sometimes > unusable);
- off-table actions count as sometimes (D32) and share the group quota of at
  most one sometimes shot per storyboard; unusable is rejected with the
  registered substitute;
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


def vocab_tier(tier, config=None):
    """The tier whose grade column is read (V2-20, TASK-v2 §10.4; V2-43a: the L1 route is removed).

    Only the L2 column is read, whatever the identity says.
    """
    return "L2"


def resolve_grade(item, tier):
    """Grade of one vocab entry for one character tier, with its basis.

    L1/L2 read their own grade. Any other tier (no data) takes the
    more conservative of the two recorded grades.
    """
    grades = item["grades"]
    if tier in grades:
        return grades[tier]["grade"], tier
    worst = max((grades[key]["grade"] for key in ("L1", "L2")),
                key=lambda g: GRADE_ORDER[g])
    return worst, "L1/L2 保守档"


def replacement_for(item, tier):
    """Substitute record for an unusable entry, honoring the tier table.

    Falls back to the recorded tier rows when the current tier has none of
    its own (conservative tiers inherit the row that made the entry unusable).
    """
    rows = item.get("replacements", {})
    if tier in rows:
        return dict(rows[tier], tier=tier)
    for fallback in ("L1", "L2"):
        if fallback in rows:
            return dict(rows[fallback], tier=fallback)
    return None


def substitute_text(item, tier):
    row = replacement_for(item, tier)
    if row is None:
        return "无登记替代"
    if row.get("substitute_id") is None:
        return f"无 id；改写成“{row['rewrite']}”" + (f"（{row['note']}）" if row.get("note") else "")
    return f"{row['substitute_id']}" + (f"（{row['note']}）" if row.get("note") else "")


def classify_shots(board, tier, vocab=None):
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
            resolved = [resolve_grade(item, tier) for item in scope]
            grade = max((g for g, _ in resolved), key=lambda g: GRADE_ORDER[g])
            basis = resolved[0][1]
        else:
            grade, basis, expression_matches = "sometimes", tier if tier in ("L1", "L2") else "L1/L2 保守档", []
        shots.append({"shot": entry.get("number", index + 1), "action": action,
                      "framing": shot["framing"], "camera_angle": shot["camera_angle"],
                      "expression": shot["expression"], "vocab_ids": [item["id"] for item in hits],
                      "off_table": not hits, "grade": grade, "grade_basis": basis,
                      "expression_matches": expression_matches,
                      "action_matches_other_framing": near})
    return shots



def validate_shot_vocab(board, tier, config=None, vocab=None):
    """Enforce §2.2 vocabulary rules on a v2 storyboard; return classifications.

    Legacy (scope_version<2) boards are out of scope and return no checks.
    Raises StoryRuleViolation with the offending shot named so planner retries
    carry the exact violation, e.g. 第 3 镜：动作 running 在 L1 为不可用，替代为 18.
    """
    if board.get("scope_version") != V2_SCOPE:
        return []
    vocab = vocab or load_vocab(config)
    tier = vocab_tier(tier, config)
    by_id = {item["id"]: item for item in vocab["items"]}
    shots = classify_shots(board, tier, vocab=vocab)
    for classified in shots:
        if classified["grade"] != "unusable":
            continue
        item = by_id[classified["vocab_ids"][0]]
        problem = (f"第 {classified['shot']} 镜：动作 {classified['action']} 在 "
                   f"{classified['grade_basis']} 为不可用，替代为 {substitute_text(item, tier)}")
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
        problem = "；".join(parts) + "；每组最多 1 镜有时（含表外）"
        fix = "换用稳定组合；表外动作与“有时”项共享这 1 镜名额"
        if any(c["off_table"] for c in sometimes):
            fix += ("；表外的 pose 若是状态（如睡着、哭），把状态移到 expression，"
                    "并换一个表内体位；其余表外 pose 改成表内动作")
        raise StoryRuleViolation(
            "shot_vocab_sometimes_limit",
            [f"$.shots[{c['shot'] - 1}].shot.pose" for c in sometimes],
            problem,
            fix)
    return shots


def _combo(item):
    return f"{item['id']} {item['framing']}/{item['camera_angle']} {item['action_tags'][0]}"


def prompt_section(tier, config=None, vocab=None):
    """Instruction block listing this tier's stable/sometimes/unusable combos."""
    vocab = vocab or load_vocab(config)
    items = vocab["items"]
    tier = vocab_tier(tier, config)
    resolved = [(item, resolve_grade(item, tier)) for item in items]
    header = (" Shot vocabulary for this character tier from the capability probe "
              "(a shot matches an entry when framing, camera_angle and the pose tag all agree; "
              "the expression may differ).")
    header += " Character LoRA is off: the L2 column applies to every tier."
    stable = [f"{_combo(item)}" for item, (grade, _) in resolved if grade == "stable"]
    sometimes = [f"{_combo(item)}" for item, (grade, _) in resolved if grade == "sometimes"]
    unusable = []
    for item, (grade, _) in resolved:
        if grade != "unusable":
            continue
        unusable.append(f"{_combo(item)} -> {substitute_text(item, tier)}")
    section = [header]
    if stable:
        section.append(" STABLE combos, plan these first: " + "; ".join(stable) + ".")
    if sometimes:
        section.append(" SOMETIMES combos, at most ONE shot of this storyboard may use one: "
                       + "; ".join(sometimes) + ".")
    if unusable:
        section.append(" UNUSABLE combos, never plan these; if the story needs the beat, use the substitute: "
                       + "; ".join(unusable) + ".")
    section.append(" Actions not listed here are allowed but count as SOMETIMES and share that "
                   "single quota. When you substitute an unusable combo, record the substitution "
                   "in story_note as one concise Chinese sentence.")
    return "".join(section)


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
    themselves. Tier comes from the saved identity; runtime review conclusion
    and evidence image come from the finished cycle or state.
    """
    root = Path(job_dir)
    board_path = root / "storyboard.json"
    if not board_path.exists():
        return []
    board = json.loads(board_path.read_text(encoding="utf-8"))
    if board.get("scope_version") != V2_SCOPE:
        return []
    vocab = vocab or load_vocab(config)
    identity_path = root / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.exists() else {}
    tier = identity.get("identity_tier")
    column = vocab_tier(tier, config)
    recorded = []
    for classified in classify_shots(board, column, vocab=vocab):
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
            "tier": tier,
            "grade_column": column,
            "grade": classified["grade"],
            "grade_basis": classified["grade_basis"],
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
