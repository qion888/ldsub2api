#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_root="${1:-$(cd "$artifact_dir/../.." && pwd)}"
diff_file="$artifact_dir/DIFF_FILE"

git -C "$target_root" apply --reverse --check "$diff_file"
git -C "$target_root" apply --reverse "$diff_file"
