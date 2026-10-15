#!/usr/bin/env bash
# Run by supervisor. Check only: never copies, links or deletes anything (V2-59, D96).
set -euo pipefail
if [[ $# != 0 ]]; then
  printf '用法：%s\n' "$0" >&2
  exit 2
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$project_root/skill/story-cg"
link="$HOME/.agents/skills/story-cg"
duplicate="$HOME/.hermes/skills/story-cg"
status=0
if [[ ! -e "$link" && ! -L "$link" ]]; then
  printf '未找到 %s。请执行：ln -s %s %s（脚本不替你执行）\n' "$link" "$source_dir" "$link" >&2
  status=1
elif [[ "$(readlink -f -- "$link")" != "$(readlink -f -- "$source_dir")" || ! -d "$link" ]]; then
  printf '%s 指向的真实路径与项目不一致（或目录不存在）：\n  实际：%s\n  应为：%s\n' \
    "$link" "$(readlink -f -- "$link")" "$(readlink -f -- "$source_dir")" >&2
  status=1
else
  printf '已就绪：%s -> %s\n' "$link" "$(readlink -f -- "$link")"
fi
if [[ -e "$duplicate" || -L "$duplicate" ]]; then
  printf '发现重复的 story-cg，Hermes 会拒绝加载；请删除 ~/.hermes/skills/story-cg\n' >&2
  status=1
fi
if command -v hermes >/dev/null 2>&1; then
  # Same rule as before: a failing list is an error; a list without story-cg is only reported.
  listing=$(hermes skills list) || { printf 'hermes skills list 失败，请监工检查。\n' >&2; exit 1; }
  printf '%s\n' "$listing" | awk '/story-cg/ {print; found=1} END {if (!found) print "hermes skills list 未显示 story-cg，请监工检查。"}'
else
  printf '未发现 hermes 命令；请在 Hermes 环境执行 hermes skills list。\n' >&2
fi
exit "$status"
