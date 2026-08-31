#!/usr/bin/env bash
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASELINE_FILE="$SCRIPT_DIR/ROLLBACK_BASELINE"
TARGET_FILE=${1:-"$SCRIPT_DIR/MODIFIED_FILE"}

if [ ! -f "$BASELINE_FILE" ]; then
  printf 'Missing rollback baseline: %s\n' "$BASELINE_FILE" >&2
  exit 2
fi

mkdir -p "$(dirname -- "$TARGET_FILE")"
cp -- "$BASELINE_FILE" "$TARGET_FILE"
printf 'RESTORED %s\n' "$TARGET_FILE"
