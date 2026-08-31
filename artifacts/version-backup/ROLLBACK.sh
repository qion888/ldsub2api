#!/usr/bin/env sh
set -eu

target="${1:-backend/version_control/service.py}"
baseline="${2:-artifacts/version-backup/ROLLBACK_BASELINE}"
cp "$baseline" "$target"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$target"
else
  shasum -a 256 "$target"
fi
