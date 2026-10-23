#!/usr/bin/env bash
# Deploys custom_components/tonie_sync via ssh to /config/custom_components/tonie_sync on a
# Home Assistant OS host (needs the "Terminal & SSH" add-on). Host: $HAOS_HOST (default: haos).
# Usage: ./deploy.sh [--restart]   (without --restart: copy + ha core check only)
set -euo pipefail
cd "$(dirname "$0")"

restart=0
case "${1:-}" in
  "") ;;
  --restart) restart=1 ;;
  *) echo "Usage: $0 [--restart]" >&2; exit 2 ;;
esac

host=${HAOS_HOST:-haos}
src=custom_components/tonie_sync
dst=/config/custom_components/tonie_sync

python3 -m py_compile "$src"/*.py
find "$src" -name __pycache__ -prune -exec rm -rf {} +

echo "→ copying to $host:$dst"
tar -C custom_components --exclude=__pycache__ -cf - tonie_sync | ssh "$host" "
  set -e
  rm -rf $dst.new && mkdir -p $dst.new
  tar -C $dst.new --strip-components=1 -xf -
  rm -rf $dst.old
  if [ -d $dst ]; then mv $dst $dst.old; fi
  mv $dst.new $dst
  rm -rf $dst.old
"

echo "→ ha core check"
ssh "$host" ha core check

if [ "$restart" = 1 ]; then
  echo "→ ha core restart"
  ssh "$host" ha core restart
else
  echo "Done. Takes effect after a restart: $0 --restart (or ssh $host ha core restart)"
fi
