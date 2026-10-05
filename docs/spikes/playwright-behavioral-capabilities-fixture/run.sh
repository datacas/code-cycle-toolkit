#!/usr/bin/env bash
# usage: run.sh <name> <playwright args...>
name=$1; shift
rm -rf out/$name; mkdir -p out/$name
PLAYWRIGHT_JSON_OUTPUT_NAME=out/$name/playwright.json rtk proxy npx playwright test --reporter=line,json --output=out/$name/artifacts "$@" > out/$name/stdout.txt 2>&1
echo "$name exit=$? json=$([ -f out/$name/playwright.json ] && echo yes || echo no)" | tee -a out/exitcodes.txt
