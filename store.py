"""store.py - append-only segment writer + SQLite index.

A *segment* is one video holding a batch of fixed-size (512 byte) records.
The SQLite index maps record id -> (segment, byte offset). Nothing in a segment
is ever modified: updates are queued, then ``flush`` writes them into a brand-new
segment and repoints the index. Old segments are never touched.

CLI:
    python store.py segments
    python store.py register --segment seg_0001 --video-id <YouTube id or URL>
    python store.py flush
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sqlite3
import struct
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import parse_qs, urlparse

from codec import Geometry, encode_blob
from video import write_video

RECORD_SIZE = 512
DEFAULT_FPS = 30
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    name       TEXT PRIMARY KEY,
    path       TEXT NOT NULL,          -- relative to the data dir
    youtube_id TEXT,                   -- set once the video has been uploaded
    frames     INTEGER NOT NULL,
    fps        INTEGER NOT NULL,
    nbytes     INTEGER NOT NULL,
    geom       TEXT NOT NULL,          -- JSON of codec.Geometry
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY,
    segment     TEXT NOT NULL REFERENCES segments(name),
    byte_offset INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id        INTEGER PRIMARY KEY,
    body      TEXT NOT NULL,
    queued_at REAL NOT NULL
);
"""


# --------------------------------------------------------------------------- records


def pack_record(obj: dict) -> bytes:
    """dict -> exactly RECORD_SIZE bytes (2-byte length prefix, JSON, zero padding)."""
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > RECORD_SIZE - 2:
        raise ValueError(f"record too large: {len(body)} bytes of JSON (max {RECORD_SIZE - 2})")
    return struct.pack(">H", len(body)) + body.ljust(RECORD_SIZE - 2, b"\0")


def unpack_record(raw: bytes) -> dict:
    if len(raw) != RECORD_SIZE:
        raise ValueError(f"expected {RECORD_SIZE} bytes, got {len(raw)}")
    (n,) = struct.unpack(">H", raw[:2])
    if n > RECORD_SIZE - 2:
        raise ValueError("corrupt record length prefix")
    return json.loads(raw[2 : 2 + n].decode("utf-8"))


def parse_video_id(value: str) -> str:
    """Accept a bare 11-char YouTube id or a watch / youtu.be URL."""
    value = value.strip()
    if YOUTUBE_ID.match(value):
        return value
    url = urlparse(value)
    candidate = (parse_qs(url.query).get("v") or [url.path.lstrip("/")])[0]
    if url.netloc and YOUTUBE_ID.match(candidate):
        return candidate
    raise ValueError(f"not a YouTube video id or URL: {value!r}")


# --------------------------------------------------------------------------- store


@dataclass(frozen=True)
class Segment:
    name: str
    path: str
    youtube_id: Optional[str]
    frames: int
    fps: int
    nbytes: int
    geom: Geometry
    created_at: float


@dataclass(frozen=True)
class Location:
    id: int
    segment: str
    offset: int
    length: int


def _segment_from_row(row: sqlite3.Row) -> Segment:
    return Segment(
        name=row["name"],
        path=row["path"],
        youtube_id=row["youtube_id"],
        frames=row["frames"],
        fps=row["fps"],
        nbytes=row["nbytes"],
        geom=Geometry(**json.loads(row["geom"])),
        created_at=row["created_at"],
    )


class Store:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.dir = Path(data_dir or os.environ.get("STATICDB_DIR", "data")).resolve()
        (self.dir / "videos").mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.sqlite3"
        self._write_lock = threading.Lock()
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.index_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ---- writing -----------------------------------------------------------

    def write_segment(
        self,
        records: Mapping[int, dict],
        *,
        geom: Geometry = Geometry(),
        fps: int = DEFAULT_FPS,
        crf: int = 18,
    ) -> Segment:
        """Encode ``records`` into a new video and point the index at it."""
        if not records:
            raise ValueError("nothing to write")
        ids = sorted(records)
        blob = b"".join(pack_record(records[i]) for i in ids)

        with self._write_lock:
            with closing(self._connect()) as db:
                n = db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] + 1
            name = f"seg_{n:04d}"
            rel = f"videos/{name}.mp4"
            frames = write_video(encode_blob(blob, geom), self.dir / rel, geom, fps=fps, crf=crf)
            now = time.time()
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?)",
                    (name, rel, None, frames, fps, len(blob), json.dumps(dataclasses.asdict(geom)), now),
                )
                db.executemany(
                    "INSERT INTO records(id, segment, byte_offset, length, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET segment=excluded.segment, "
                    "byte_offset=excluded.byte_offset, length=excluded.length, updated_at=excluded.updated_at",
                    [(rid, name, i * RECORD_SIZE, RECORD_SIZE, now) for i, rid in enumerate(ids)],
                )
        return self.segment(name)

    def register(self, segment: str, video_id: str) -> None:
        """Remember that ``segment`` has been uploaded to YouTube as ``video_id``."""
        video_id = parse_video_id(video_id)
        with closing(self._connect()) as db, db:
            cur = db.execute("UPDATE segments SET youtube_id=? WHERE name=?", (video_id, segment))
            if cur.rowcount == 0:
                raise KeyError(f"unknown segment: {segment}")

    # ---- pending updates (eventual consistency, hours) -----------------------

    def queue_update(self, record_id: int, body: dict) -> int:
        """Queue a full replacement of a record. Returns how many updates are pending."""
        body = {**body, "id": record_id}
        pack_record(body)  # validate size now, not at flush time
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO pending(id, body, queued_at) VALUES (?,?,?)",
                (record_id, json.dumps(body), time.time()),
            )
        return self.stats()["pending"]

    def flush(self, **kwargs) -> Optional[Segment]:
        """Write all pending updates into one new segment. None if there is nothing to do."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id, body, queued_at FROM pending ORDER BY id").fetchall()
        if not rows:
            return None
        segment = self.write_segment({r["id"]: json.loads(r["body"]) for r in rows}, **kwargs)
        with closing(self._connect()) as db, db:
            db.executemany(
                "DELETE FROM pending WHERE id=? AND queued_at=?",
                [(r["id"], r["queued_at"]) for r in rows],
            )
        return segment

    # ---- reading the index -------------------------------------------------------

    def locate(self, record_id: int) -> Optional[Location]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, segment, byte_offset, length FROM records WHERE id=?", (record_id,)
            ).fetchone()
        return Location(row["id"], row["segment"], row["byte_offset"], row["length"]) if row else None

    def segment(self, name: str) -> Segment:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM segments WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"unknown segment: {name}")
        return _segment_from_row(row)

    def segments(self) -> list[Segment]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM segments ORDER BY name").fetchall()
        return [_segment_from_row(r) for r in rows]

    def video_path(self, segment: Segment) -> Path:
        return self.dir / segment.path

    def stats(self) -> dict:
        with closing(self._connect()) as db:
            one = lambda q: db.execute(q).fetchone()[0]  # noqa: E731
            return {
                "segments": one("SELECT COUNT(*) FROM segments"),
                "uploaded_segments": one("SELECT COUNT(*) FROM segments WHERE youtube_id IS NOT NULL"),
                "records": one("SELECT COUNT(*) FROM records"),
                "pending": one("SELECT COUNT(*) FROM pending"),
            }


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="store.py", description="StaticDB index maintenance")
    parser.add_argument("--data-dir", help="defaults to $STATICDB_DIR or ./data")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("segments", help="list segments and their upload status")
    reg = sub.add_parser("register", help="record the YouTube id of an uploaded segment")
    reg.add_argument("--segment", required=True, help="e.g. seg_0001")
    reg.add_argument("--video-id", required=True, help="YouTube video id or URL")
    sub.add_parser("flush", help="write pending updates into a new segment")
    args = parser.parse_args(argv)

    store = Store(args.data_dir)
    if args.cmd == "segments":
        print(f"{'segment':10} {'frames':>6} {'KiB':>7}  youtube")
        for s in store.segments():
            print(f"{s.name:10} {s.frames:>6} {s.nbytes / 1024:>7.1f}  {s.youtube_id or '(local only)'}")
        print(store.stats())
    elif args.cmd == "register":
        store.register(args.segment, args.video_id)
        print(f"{args.segment} -> {parse_video_id(args.video_id)}")
    elif args.cmd == "flush":
        seg = store.flush()
        print(f"wrote {seg.name} ({seg.frames} frames): {store.video_path(seg)}" if seg else "nothing pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
