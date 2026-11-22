-- YouTubeDB / StaticDB Local SQLite Index Schema
-- Run manually if needed: sqlite3 data/index.sqlite3 < schema.sql

CREATE TABLE IF NOT EXISTS segments (
    name       TEXT PRIMARY KEY,
    path       TEXT NOT NULL,          -- relative to data dir (e.g., videos/seg_0001.mp4)
    youtube_id TEXT,                   -- 11-char YouTube ID once uploaded (NULL if local)
    frames     INTEGER NOT NULL,
    fps        INTEGER NOT NULL,
    nbytes     INTEGER NOT NULL,
    geom       TEXT NOT NULL,          -- JSON serialized codec.Geometry parameters
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY,
    segment     TEXT NOT NULL REFERENCES segments(name),
    byte_offset INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_segment ON records(segment);

CREATE TABLE IF NOT EXISTS pending (
    id        INTEGER PRIMARY KEY,
    body      TEXT NOT NULL,          -- JSON payload awaiting next flush
    queued_at REAL NOT NULL
);
