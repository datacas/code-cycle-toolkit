#!/usr/bin/env bash
# usage: run.sh <name> <playwright args...>
if [[ $# -lt 1 || ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ || $1 == . || $1 == .. ]]; then
  echo "name must contain only letters, numbers, dots, underscores, or hyphens" >&2
  exit 2
fi
name=$1; shift
out_dir="out/$name"
rm -rf "$out_dir"
mkdir -p "$out_dir"
PLAYWRIGHT_JSON_OUTPUT_NAME="$out_dir/playwright.json" rtk proxy npx playwright test --reporter=line,json --output="$out_dir/artifacts" "$@" > "$out_dir/stdout.txt" 2>&1
echo "$name exit=$? json=$([ -f "$out_dir/playwright.json" ] && echo yes || echo no)" | tee -a out/exitcodes.txt
