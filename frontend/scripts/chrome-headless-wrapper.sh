#!/usr/bin/env bash
# Wrapper that forces Google Chrome into headless=new mode so Flutter's
# `flutter drive -d chrome` / `flutter run -d chrome` works on servers
# without an X display (e.g. CI runners, this Linux dev box without sudo).
#
# Used via CHROME_EXECUTABLE=/.../chrome-headless-wrapper.sh make web-smoke-chrome
set -euo pipefail

CHROME_BIN="${TGPP_CHROME_BIN:-/usr/bin/google-chrome}"

if [ ! -x "$CHROME_BIN" ]; then
  echo "chrome-headless-wrapper: $CHROME_BIN not found / not executable" >&2
  exit 127
fi

exec "$CHROME_BIN" \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --window-size=1280,800 \
  "$@"
