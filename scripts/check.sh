#!/usr/bin/env bash
# Check source and tests. With a message and explicit paths, commit those paths and push.
#   scripts/check.sh
#   scripts/check.sh "message" PATH [PATH ...]
#   scripts/check.sh --all "message"  # src scripts tests .agents only
set -u
cd "$(dirname "$0")/.."

all=0
if [ "${1-}" = "--all" ]; then all=1; shift; fi
message="${1-}"
if [ $# -ge 1 ]; then shift; fi
paths=("$@")

if [ -n "$message" ] && [ $all -eq 0 ] && [ ${#paths[@]} -eq 0 ]; then
  echo "name what to commit: scripts/check.sh \"message\" PATH [PATH ...], or --all for the whole tree"
  exit 2
fi
if [ ${#paths[@]} -gt 0 ]; then
  for path in "${paths[@]}"; do
    if [ ! -e "$path" ]; then echo "no such path: $path; nothing committed"; exit 2; fi
  done
  # What ruff may rewrite: the caller's own Python files and directories, and nothing else.
  fixable=()
  for path in "${paths[@]}"; do
    case "$path" in
      *.py) fixable+=("$path") ;;
      *) if [ -d "$path" ]; then fixable+=("$path"); fi ;;
    esac
  done
  if [ ${#fixable[@]} -gt 0 ]; then
    .venv/bin/ruff check "${fixable[@]}" --fix -q && .venv/bin/ruff format -q "${fixable[@]}" \
      || { echo "lint failed"; exit 1; }
  fi
fi
.venv/bin/ruff check src scripts tests -q || { echo "lint failed"; exit 1; }
out=$(mktemp)
.venv/bin/python -m pytest tests -q -p no:cacheprovider > "$out" 2>&1
rc=$?
tail -1 "$out"
if [ $rc -ne 0 ]; then grep -E "^FAILED|^ERROR" "$out" | head -10; echo "tests failed (rc=$rc); nothing committed"; rm -f "$out"; exit $rc; fi
rm -f "$out"
if [ -z "$message" ]; then exit 0; fi
if [ $all -eq 1 ]; then
  paths=(src scripts tests .agents)
fi
git add -- "${paths[@]}" || { echo "git add failed; nothing committed"; exit 1; }
if git diff --cached --quiet -- "${paths[@]}"; then
  echo "nothing staged under ${paths[*]}; nothing committed"
  exit 2
fi
git commit -q -m "$message" -- "${paths[@]}" \
  && git push -q origin main && git log --oneline -1
# Show source changes outside the requested commit.
left=$(git status --porcelain -- src scripts tests .agents config.toml | head -20)
if [ -n "$left" ]; then echo "not committed (still dirty):"; echo "$left"; fi
