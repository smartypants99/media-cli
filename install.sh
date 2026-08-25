#!/usr/bin/env bash
# Installs the `media` CLI onto your PATH.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${1:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sf "$HERE/scripts/media" "$BIN/media"
chmod +x "$HERE/scripts/media" "$HERE/scripts"/*.py 2>/dev/null || true
echo "linked $BIN/media -> $HERE/scripts/media"
for t in ffmpeg ffprobe mkvmerge python3 node; do
  command -v "$t" >/dev/null || echo "  MISSING: $t"
done
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  add to PATH:  export PATH=\"$BIN:\$PATH\"" ;; esac
echo "next:  media keys set premiumize   (optional but much faster)"
echo "       media ai \"get me avatar season 2 in good quality\""
