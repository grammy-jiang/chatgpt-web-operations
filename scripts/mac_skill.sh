#!/bin/zsh
# MacBook Pro local-only ChatGPT Chat Mode Skill entry point.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "mac_skill: only for macOS; Raspberry Pi uses the original scripts" >&2
  exit 2
fi
if (( $# < 1 )); then
  echo "usage: mac_skill.sh script.py [script arguments...]" >&2
  exit 2
fi
SCRIPT="$1"
shift
if [[ ! "$SCRIPT" =~ '^[a-z][a-z0-9_]*\.py$' ]] || [[ ! -f "$ROOT/scripts/$SCRIPT" ]]; then
  echo "mac_skill: invalid bundled script" >&2
  exit 2
fi
export RP_BROWSER_CDP="http://127.0.0.1:9233"
export RP_DEDICATED_PORT=9233
export RP_BROWSER_TAB=new
export RP_MAX_BROWSERS=1
export CHATGPT_SESSION_STORE=0
export CHATGPT_MACOS_CDP_MODE=1
if ! /usr/bin/curl -fsS --max-time 4 "$RP_BROWSER_CDP/json/version" >/dev/null; then
  echo "mac_skill: dedicated Mac Chrome CDP unavailable on loopback:9233" >&2
  exit 1
fi
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/$SCRIPT" "$@"
