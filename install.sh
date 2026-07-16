#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE_DIR="$HOME/.local/share/smash"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$SHARE_DIR" "$BIN_DIR"
cp "$SRC_DIR/smash.py" "$SHARE_DIR/smash.py"
chmod 755 "$SHARE_DIR/smash.py"
ln -sf "$SHARE_DIR/smash.py" "$BIN_DIR/smash"

echo "installed: $SHARE_DIR/smash.py"
echo "launcher : $BIN_DIR/smash -> $SHARE_DIR/smash.py"
