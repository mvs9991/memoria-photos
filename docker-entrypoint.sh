#!/bin/sh
# Make `docker compose up` do the obvious thing: if a library is mounted at /photos,
# register it, so the app has something to show instead of an empty page.
#
# Registering is idempotent (roots.path is unique), and it only records the path —
# nothing is read or written in /photos here. Indexing stays a deliberate act, started
# from the UI or `docker exec memoria python -m photointel index`, because on a large
# library it is long and better watched.
set -e

if [ -d /photos ]; then
    if ! python -m photointel add-root /photos >/dev/null 2>&1; then
        echo "note: could not register /photos as a library root; add it in the UI." >&2
    fi
fi

exec "$@"
