#!/bin/sh
# Starts as root only long enough to make the data directory writable (hosted volumes
# are mounted root-owned), then drops to the unprivileged "flow" user.
set -e
DATA_DIR="$(dirname "${FLOW_DB_PATH:-/home/flow/var/flow.db}")"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown -R flow:flow "$DATA_DIR"
  exec setpriv --reuid=flow --regid=flow --init-groups "$@"
fi
exec "$@"
