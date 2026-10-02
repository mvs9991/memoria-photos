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
    # Only the top level: chown -R over a large existing library would be slow, and the
    # files underneath were written by this same user anyway.
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

exec "$@"
