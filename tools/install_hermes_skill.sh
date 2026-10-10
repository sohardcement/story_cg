#!/usr/bin/env bash
# Run by supervisor; no Hermes global configuration changes.
set -euo pipefail
force=false
if [[ ${1:-} == --force && $# == 1 ]]; then
  force=true
elif [[ $# != 0 ]]; then
  printf '用法：%s [--force]\n' "$0" >&2
  exit 2
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$project_root/skill/story-cg"
target="$HOME/.hermes/skills/story-cg"
if [[ -e "$target" || -L "$target" ]]; then
  if [[ "$force" != true ]]; then
    printf '拒绝覆盖已有目标：%s；明确需要替换时使用 --force。\n' "$target" >&2
    exit 1
  fi
  printf '警告：--force 将替换 %s（不修改全局配置）。\n' "$target" >&2
  if [[ -L "$target" || ! -d "$target" ]]; then
    printf '拒绝替换符号链接或非目录目标。\n' >&2
    exit 1
  fi
fi
mkdir -p -- "$(dirname -- "$target")"
staging=$(mktemp -d "$(dirname -- "$target")/.story-cg.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
cp -R -- "$source_dir/." "$staging/"
printf '%s\n' "$project_root" > "$staging/project_path"
chmod +x "$staging/scripts/storycg"
if [[ -d "$target" ]]; then rm -rf -- "$target"; fi
mv -- "$staging" "$target"
printf '已安装：%s\n' "$target"
if command -v hermes >/dev/null 2>&1; then
  # Preserve command failures; list can show whether Hermes discovers the skill.
  listing=$(hermes skills list) || { printf 'hermes skills list 失败，请监工检查。\n' >&2; exit 1; }
  printf '%s\n' "$listing" | awk '/story-cg/ {print; found=1} END {if (!found) print "hermes skills list 未显示 story-cg，请监工检查。"}'
else
  printf '未发现 hermes 命令；安装文件已准备，请在 Hermes 环境执行 hermes skills list。\n' >&2
fi
