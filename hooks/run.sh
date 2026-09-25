#!/usr/bin/env bash
# Portable launcher for the second-brain hooks.
#
# Plain words: finds a working Python and runs the hook script, so the same
# committed config works on this Windows machine and on the Linux box a cloud
# routine runs on. Without this, the repo's hook config would name a Windows
# python.exe that does not exist in the cloud.
#
# Usage (from .claude/settings.json):
#   command: ${CLAUDE_PROJECT_DIR}/hooks/run.sh
#   args:    ["inject_memory.py"]
#
# Never fails loudly: a hook that errors on every prompt is worse than a hook
# that silently does nothing.

set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/${1:-}"
[ -f "$script" ] || exit 0
shift || true

# Being on PATH is NOT proof of being Python. On Windows, `python3` resolves to
# the Microsoft Store stub, which exists, prints an error, and exits 49. So each
# candidate must actually execute before we hand it the script.
for candidate in "${SECOND_BRAIN_PYTHON:-}" python3 python py; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  "$candidate" -c "import sys" >/dev/null 2>&1 || continue
  exec "$candidate" "$script" "$@"
done

exit 0  # no interpreter available: stay silent rather than break the session
