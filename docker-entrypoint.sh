#!/bin/sh
# Runs as root only long enough to make the data volume writable, then drops to the unprivileged
# "clipper" user (uid 1000) for the application itself. Hosting platforms such as Railway mount
# volumes owned by root; this avoids having to run the whole app as root.
set -e

DATA="${STORAGE_ROOT:-${FC_DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-/data}}}"
APP_UID=1000
APP_GID=1000

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  [ "$(stat -c %u "$DATA")" = "$APP_UID" ] || chown "$APP_UID:$APP_GID" "$DATA"
  # Only top-level entries not owned by the app user (e.g. lost+found, files restored by root) are fixed,
  # so start-up stays fast on a large volume.
  find "$DATA" -mindepth 1 -maxdepth 1 ! -user "$APP_UID" -exec chown -R "$APP_UID:$APP_GID" {} +
  if [ -n "$DATABASE_PATH" ]; then
    mkdir -p "$(dirname "$DATABASE_PATH")"
    chown "$APP_UID:$APP_GID" "$(dirname "$DATABASE_PATH")"
  fi
  exec env HOME=/home/clipper setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
fi

exec "$@"
