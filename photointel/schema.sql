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
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error | missing
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
