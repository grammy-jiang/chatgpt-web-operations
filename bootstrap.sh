#!/usr/bin/env bash
# Create this skill's virtual environment, .venv, next to this script.
# Idempotent: re-run it after editing requirements.txt. Installs nothing
# system-wide; the one system dependency (python3-dbus) is checked, not installed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PYTHON="${CHATGPT_WEB_OPS_PYTHON:-/usr/bin/python3}"

if ! "$PYTHON" -c 'import dbus' 2>/dev/null; then
  echo "bootstrap: $PYTHON cannot import dbus." >&2
  echo "The cookie decryptor reads the GNOME keyring over D-Bus. Install the" >&2
  echo "system package (Debian / Raspberry Pi OS: sudo apt install python3-dbus)." >&2
  echo "This script does not install it, and pip cannot build it cleanly." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PYTHON" --system-site-packages "$VENV"
  else
    "$PYTHON" -m venv --system-site-packages "$VENV"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" -r "$HERE/requirements.txt"
else
  "$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"
fi

"$VENV/bin/python" - <<'EOF'
import cryptography, dbus, playwright  # noqa: F401
print("bootstrap: dbus, cryptography and playwright import inside .venv")
EOF
echo "bootstrap: $VENV is ready"
