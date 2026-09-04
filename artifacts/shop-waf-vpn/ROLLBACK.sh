#!/usr/bin/env bash
set -euo pipefail

repo="${1:-$(pwd)}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
patch_file="${2:-${script_dir}/DIFF_FILE}"

git -C "$repo" apply --reverse --check "$patch_file"
git -C "$repo" apply --reverse "$patch_file"
printf 'ROLLBACK_OK repo=%s patch=%s\n' "$repo" "$patch_file"
