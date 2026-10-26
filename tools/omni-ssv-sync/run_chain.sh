#!/usr/bin/env bash
# usage: run_chain.sh <topic file> <existing ssv yaml> <out yaml> [--prune]
# OSSIE_CONVERTERS points at a checkout of apache/ossie-converters (with submodules).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; conv="${OSSIE_CONVERTERS:-$here/ossie-converters}"
topic="$1"; existing="$2"; out="$3"; shift 3
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
uv run -q --with pyyaml python "$here/hoist_topic_views.py" "$topic" "$tmp/omni"
(cd "$conv/omni" && uv run -q ossie-omni import -i "$tmp/omni" -o "$tmp/model.ossie.yaml" --name topic >/dev/null 2>&1)
(cd "$conv/snowflake" && uv run -q ossie-snowflake -i "$tmp/model.ossie.yaml" -o "$tmp/generated.yaml" >/dev/null 2>&1)
uv run -q "$here/merge_sv.py" --existing "$existing" --generated "$tmp/generated.yaml" --out "$out" "$@"
