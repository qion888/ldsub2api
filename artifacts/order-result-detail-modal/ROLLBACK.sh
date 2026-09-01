#!/usr/bin/env sh
set -eu
artifact_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=${1:-"$artifact_dir/rollback-test/OrderQueryView.jsx"}
mkdir -p "$(dirname -- "$target")"
cp "$artifact_dir/BASELINE_FILE" "$target"
printf 'restored %s\n' "$target"
