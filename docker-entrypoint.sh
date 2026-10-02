#!/bin/sh
# Container start-up.
#
# Runs as root only long enough to make /data writable, then drops to the unprivileged
# `memoria` user. A bind-mounted /data is created by Docker owned by root, so without
# this the app cannot write its own cache; a named volume inherits the image's
# ownership and would have worked either way.
set -e

if [ "$(id -u)" = "0" ]; then
    chown memoria:memoria /data 2>/dev/null || true
    # Only the top level: chown -R over a large library would be slow, and anything
    # underneath was written by this same user anyway.
    exec gosu memoria "$0" "$@"
fi

# Register a mounted library so the app has something to show rather than an empty page.
# Idempotent (roots.path is unique) and it only records the path — nothing in /photos is
# read or written here. Indexing stays deliberate: start it from the UI, or
#   docker exec memoria python -m photointel index
if [ -d /photos ]; then
    if ! python -m photointel add-root /photos >/dev/null 2>&1; then
        echo "note: could not register /photos as a library root; add it in the UI." >&2
    fi
fi

# The container binds 0.0.0.0, so the only thing keeping the library private is the
# publish mapping. Say so plainly while that is the case.
if ! python - <<'PY' 2>/dev/null
import sys
from photointel.context import AppContext
from photointel.accounts import enabled
ctx = AppContext(None)
c = ctx.connect()
ok = bool(ctx.settings.access_password_hash) or enabled(c)
c.close()
sys.exit(0 if ok else 1)
PY
then
    echo "-------------------------------------------------------------------" >&2
    echo " No password is set. This is safe only while the port is published" >&2
    echo " to 127.0.0.1 (the default in docker-compose.yml)." >&2
    echo " Before reaching Memoria from other devices, set one:" >&2
    echo "   docker exec -it memoria python -m photointel set-password" >&2
    echo "-------------------------------------------------------------------" >&2
fi

exec "$@"
