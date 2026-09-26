-- Memoria library schema (version 1).
-- Timestamps: `*_at` columns are UNIX epoch seconds (UTC).
-- `taken_ts` is the photo's *wall-clock* capture time encoded as if it were UTC
-- (i.e. calendar grouping never shifts with the server's timezone).

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS roots (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    added_at     REAL NOT NULL,
    last_scan_at REAL
);

-- Registry of every model whose outputs are stored. Outputs are always tagged
-- with model_id so incompatible embeddings are never mixed.
CREATE TABLE IF NOT EXISTS models (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,          -- face | semantic | caption
    name       TEXT NOT NULL,
    version    TEXT NOT NULL,
    dim        INTEGER,
    params     TEXT,                   -- JSON
    created_at REAL NOT NULL,
    UNIQUE(kind, name, version)
);

CREATE TABLE IF NOT EXISTS places (
    id           INTEGER PRIMARY KEY,
    geoname_id   INTEGER UNIQUE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,        -- city | locality | landmark
    city         TEXT,
    admin2       TEXT,
    admin1       TEXT,
    country_code TEXT,
    country      TEXT,
    lat          REAL,
    lon          REAL,
    population   INTEGER
);

CREATE TABLE IF NOT EXISTS photos (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id),
    rel_path        TEXT NOT NULL,
    folder          TEXT NOT NULL,
    filename        TEXT NOT NULL,
    ext             TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    ctime           REAL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error | missing | trashed | deleted
    error           TEXT,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL,

    sha256          TEXT,
    phash           INTEGER,
    dhash           INTEGER,

    width           INTEGER,
    height          INTEGER,
    orientation     INTEGER,
    format          TEXT,

    taken_ts        REAL,
    taken_local     TEXT,
    tz_offset_min   INTEGER,
    date_source     TEXT,              -- exif | exif_digitized | filename | folder | mtime
    date_confidence TEXT,              -- high | medium | low

    camera_make     TEXT,
    camera_model    TEXT,
    lens            TEXT,
    focal_length    REAL,
    aperture        REAL,
    exposure_time   REAL,
    iso             INTEGER,
    software        TEXT,

    gps_lat         REAL,
    gps_lon         REAL,
    gps_alt         REAL,
    place_id        INTEGER REFERENCES places(id),
    landmark_id     INTEGER REFERENCES places(id),
    location_source TEXT,              -- gps | event | nearby_time | folder | visual
    location_confidence TEXT,          -- high | medium | low | unknown

    source_kind     TEXT,              -- camera | phone | whatsapp | screenshot | download | edited | scan | unknown

    blur            REAL,              -- variance of Laplacian (higher = sharper)
    brightness      REAL,
    contrast        REAL,
    clipped         REAL,              -- fraction of clipped pixels
    quality_score   REAL,              -- 0..100 combined
    aesthetic       REAL,              -- 0..1 zero-shot aesthetic proxy
    face_count      INTEGER,

    caption         TEXT,
    caption_model   INTEGER REFERENCES models(id),

    meta_version    INTEGER,           -- CPU analysis version completed (NULL = pending)
    faces_model     INTEGER REFERENCES models(id),
    semantic_model  INTEGER REFERENCES models(id),

    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL,
    favorite        INTEGER NOT NULL DEFAULT 0,
    hidden          INTEGER NOT NULL DEFAULT 0,

    UNIQUE(root_id, rel_path)
);
CREATE INDEX IF NOT EXISTS ix_photos_taken   ON photos(taken_ts);
CREATE INDEX IF NOT EXISTS ix_photos_sha     ON photos(sha256);
CREATE INDEX IF NOT EXISTS ix_photos_status  ON photos(status);
CREATE INDEX IF NOT EXISTS ix_photos_event   ON photos(event_id);
CREATE INDEX IF NOT EXISTS ix_photos_place   ON photos(place_id);
CREATE INDEX IF NOT EXISTS ix_photos_folder  ON photos(root_id, folder);

CREATE TABLE IF NOT EXISTS photo_embeddings (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    model_id INTEGER NOT NULL REFERENCES models(id),
    vec      BLOB NOT NULL,            -- float16, L2-normalised
    PRIMARY KEY (photo_id, model_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS persons (
    id                 INTEGER PRIMARY KEY,
    name               TEXT,
    display_no         INTEGER,                     -- stable "Person 007" label for unnamed people
    hidden             INTEGER NOT NULL DEFAULT 0,  -- user hid from People
    ignored            INTEGER NOT NULL DEFAULT 0,  -- strangers / not interesting; excluded from search
    cover_face_id      INTEGER,
    face_count         INTEGER NOT NULL DEFAULT 0,
    photo_count        INTEGER NOT NULL DEFAULT 0,
    first_seen_ts      REAL,
    last_seen_ts       REAL,
    cluster_confidence REAL,
    face_model         INTEGER REFERENCES models(id),
    merged_into        INTEGER REFERENCES persons(id),
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_persons_name ON persons(name);

CREATE TABLE IF NOT EXISTS faces (
    id                INTEGER PRIMARY KEY,
    photo_id          INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    model_id          INTEGER NOT NULL REFERENCES models(id),
    x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,  -- normalised [0,1] of oriented image
    landmarks         TEXT,            -- JSON [x0,y0,...,x4,y4] normalised
    det_score         REAL NOT NULL,
    size_px           REAL NOT NULL,   -- min(box w,h) in original pixels
    sharpness         REAL,
    yaw               REAL,            -- rough frontal-ness proxy in [-1, 1]
    quality           REAL NOT NULL,   -- 0..1
    embedding         BLOB NOT NULL,   -- float16, L2-normalised
    person_id         INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    assign_source     TEXT,            -- cluster | attach | user
    assign_confidence REAL,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_faces_photo  ON faces(photo_id);
CREATE INDEX IF NOT EXISTS ix_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS ix_faces_model  ON faces(model_id);

-- "This face is NOT this person" — a hard constraint for clustering.
CREATE TABLE IF NOT EXISTS face_rejections (
    face_id    INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    PRIMARY KEY (face_id, person_id)
) WITHOUT ROWID;

-- Pairs of persons the user said are different (suppresses merge suggestions).
CREATE TABLE IF NOT EXISTS person_not_same (
    a INTEGER NOT NULL, b INTEGER NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (a, b)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY,
    parent_id           INTEGER REFERENCES events(id) ON DELETE SET NULL,  -- trip containing this event
    kind                TEXT NOT NULL,          -- event | trip
    auto_title          TEXT NOT NULL,
    user_title          TEXT,
    category            TEXT,
    category_confidence REAL,
    start_ts            REAL NOT NULL,
    end_ts              REAL NOT NULL,
    photo_count         INTEGER NOT NULL,
    people_count        INTEGER NOT NULL DEFAULT 0,
    place_id            INTEGER REFERENCES places(id),
    places_json         TEXT,
    location_confidence TEXT,
    cover_photo_id      INTEGER,
    summary             TEXT,
    summary_source      TEXT,                   -- template | caption | llm
    folder_hint         TEXT,
    lat                 REAL,
    lon                 REAL,
    signature           TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_start  ON events(start_ts);
CREATE INDEX IF NOT EXISTS ix_events_parent ON events(parent_id);

-- Trips contain events; photos belong to the leaf event via photos.event_id.
-- For fast "events of a trip" / "trip of a photo" we also keep membership here.
CREATE TABLE IF NOT EXISTS trip_photos (
    trip_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    PRIMARY KEY (trip_id, photo_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_trip_photos_photo ON trip_photos(photo_id);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL          -- scene | object | activity | event | environment | landmark
);

CREATE TABLE IF NOT EXISTS photo_tags (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    score    REAL NOT NULL,
    source   TEXT NOT NULL,         -- semantic | caption | user | llm
    PRIMARY KEY (photo_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_photo_tags_tag ON photo_tags(tag_id, score);

CREATE TABLE IF NOT EXISTS dup_groups (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,   -- exact | near | likely | similar
    member_count   INTEGER NOT NULL,
    keep_photo_id  INTEGER,
    review_status  TEXT NOT NULL DEFAULT 'pending',  -- pending | reviewed | not_duplicate
    signature      TEXT NOT NULL UNIQUE,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_members (
    group_id   INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    photo_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    relation   TEXT,                -- original | exact | resized | compressed | edited | cropped | screenshot | similar
    similarity REAL,
    hamming    INTEGER,
    PRIMARY KEY (group_id, photo_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_dup_members_photo ON dup_members(photo_id);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,     -- queued | running | done | failed | cancelled | interrupted
    params        TEXT,
    stage         TEXT,
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    message       TEXT,
    error         TEXT,
    pid           INTEGER,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    heartbeat_at  REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS processing_errors (
    id        INTEGER PRIMARY KEY,
    photo_id  INTEGER REFERENCES photos(id) ON DELETE CASCADE,
    path      TEXT,
    stage     TEXT NOT NULL,
    error     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_errors_photo ON processing_errors(photo_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    created_at  REAL NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    details     TEXT,
    actor       TEXT NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity_type, entity_id);

-- Keyword search over filenames, folders, captions, tags, places, people and events.
CREATE VIRTUAL TABLE IF NOT EXISTS photo_fts USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2'
);

-- ---- v2 ----------------------------------------------------------------------------------
-- Columns added to existing tables live in db.py (_ensure_columns): an ALTER must not be
-- attempted by this script on a database that already has them.

-- Words that appear *in* a photo (OCR) and descriptions people wrote. Kept apart from
-- photo_fts so a quoted text search never matches a filename or folder instead.
CREATE VIRTUAL TABLE IF NOT EXISTS photo_text_fts USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS albums (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    source         TEXT NOT NULL DEFAULT 'user',   -- user | takeout
    source_key     TEXT UNIQUE,                    -- takeout: '<root_id>:<folder>'; keeps re-imports idempotent
    cover_photo_id INTEGER,
    hidden         INTEGER NOT NULL DEFAULT 0,     -- a deleted imported album: kept so re-import skips it
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS album_photos (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, photo_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_album_photos_photo ON album_photos(photo_id);

-- What a Google Takeout .json sidecar said about a photo. Stored as evidence, not as truth:
-- which fields are *used* is decided in engine/takeout.py (see HANDOFF.md).
CREATE TABLE IF NOT EXISTS takeout_sidecars (
    photo_id    INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    json_path   TEXT NOT NULL,
    taken_ts    REAL,
    lat         REAL,
    lon         REAL,
    description TEXT,
    people      TEXT,                              -- JSON list of names
    favorited   INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    trashed     INTEGER NOT NULL DEFAULT 0,
    imported_at REAL NOT NULL
);

-- ---- v3 ----------------------------------------------------------------------------------
-- Corrections the user made to a photo's date or place. Kept apart from the values read
-- from the file so a re-index (which rewrites those) can re-apply them. Never written to
-- the original.
CREATE TABLE IF NOT EXISTS photo_overrides (
    photo_id    INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    taken_local TEXT,
    lat         REAL,
    lon         REAL,
    updated_at  REAL NOT NULL
);

-- ---- v4 ----------------------------------------------------------------------------------
-- Read-only links to one album, for people without a login. Revocable; optional expiry.
CREATE TABLE IF NOT EXISTS share_links (
    token          TEXT PRIMARY KEY,               -- unguessable, 128-bit
    album_id       INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    allow_download INTEGER NOT NULL DEFAULT 0,
    expires_at     REAL,
    created_at     REAL NOT NULL,
    last_used_at   REAL
);

-- GPS tracks (.gpx) from a phone, watch or logger: drawn on the map and used to place
-- photos that have no GPS of their own (a camera without GPS carried alongside).
CREATE TABLE IF NOT EXISTS gpx_tracks (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,                -- where it was found or imported to
    name      TEXT,
    start_ts  REAL NOT NULL,                       -- true UTC epoch seconds (GPX times are UTC)
    end_ts    REAL NOT NULL,
    points    BLOB NOT NULL,                       -- float64 triples (utc_ts, lat, lon)
    n_points  INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    added_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gpx_time ON gpx_tracks(start_ts, end_ts);

-- Trash: files the user explicitly deleted. Each is *renamed* (same drive, never copied)
-- into <data>/trash, or <root>/.memoria-trash when the photos are on another drive, and
-- kept for Settings.trash_days before being erased. Restoring renames it back. No FK to
-- photos: an entry must outlive its photo row so the file is still purged.
CREATE TABLE IF NOT EXISTS trash (
    id          INTEGER PRIMARY KEY,
    photo_id    INTEGER NOT NULL,
    files       TEXT NOT NULL,                     -- JSON [[original_path, trash_path], ...]
    size        INTEGER NOT NULL,
    prev_status TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',   -- pending | trashed | restored | purged
    trashed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_trash_state ON trash(state, expires_at);
CREATE INDEX IF NOT EXISTS ix_trash_photo ON trash(photo_id);

-- Files added through the app (phone uploads, shared-album contributions): who, and
-- the hash, so the same photo uploaded twice before indexing is stored once.
CREATE TABLE IF NOT EXISTS uploads (
    sha256   TEXT PRIMARY KEY,
    path     TEXT NOT NULL,
    who      TEXT,
    added_at REAL NOT NULL
);

-- What an iCloud Photos export said about a photo (flags are applied once; see engine/icloud.py).
CREATE TABLE IF NOT EXISTS icloud_items (
    photo_id    INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    created_ts  REAL,
    favorite    INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    imported_at REAL NOT NULL
);

-- Accounts (optional): with none, the library is one person's, guarded by one password.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'family',      -- owner | family | guest
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login_at REAL,
    pw_changed_at REAL NOT NULL DEFAULT 0           -- sessions issued before this are refused
);

-- Edited copies and trimmed videos made in the app (the original is only ever read).
CREATE TABLE IF NOT EXISTS edits (
    id          INTEGER PRIMARY KEY,
    original_id INTEGER NOT NULL,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,                     -- photo | trim
    created_at  REAL NOT NULL
);
