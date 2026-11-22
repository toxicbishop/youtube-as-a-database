"""reader.py - fetch one record: index lookup -> video section -> decode -> slice.

Sources (``STATICDB_SOURCE`` env var or the ``source`` argument):
    local    read the segment mp4 from the data dir (default; great for development)
    youtube  download only the needed seconds from YouTube with yt-dlp
             (segments that have not been registered/uploaded fall back to local)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from codec import decode_frames
from store import YOUTUBE_ID, Store, unpack_record
from video import _ffmpeg, read_video

LOCAL_PAD = 0.25  # seconds of slack around the wanted frames (local files seek accurately)
YOUTUBE_PAD = 2.0  # YouTube re-encodes, so section cuts land on its own keyframes


class RecordNotFound(KeyError):
    """No such record id in the index."""


class RecordUnavailable(RuntimeError):
    """The record exists but its frames could not be fetched or decoded."""


def download_section(youtube_id: str, start: float, end: float, out_dir: str | Path) -> Path:
    """Download only [start, end] seconds of a YouTube video with yt-dlp."""
    if not YOUTUBE_ID.match(youtube_id):
        raise RecordUnavailable(f"refusing odd-looking video id: {youtube_id!r}")
    out = Path(out_dir)
    cmd = [
        sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist",
        "--ffmpeg-location", _ffmpeg(),
        "-f", "bv*[height<=1080]/bv*",
        "--download-sections", f"*{start:.2f}-{end:.2f}",
        "-o", str(out / "section.%(ext)s"),
        f"https://www.youtube.com/watch?v={youtube_id}",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True)
    files = sorted(out.glob("section.*"))
    if proc.returncode != 0 or not files:
        raise RecordUnavailable(f"yt-dlp failed: {proc.stderr.strip()[-300:] or 'no output file'}")
    return files[0]


def fetch_record(store: Store, record_id: int, source: Optional[str] = None) -> tuple[dict, dict]:
    """Return ``(record, meta)``. Raises RecordNotFound / RecordUnavailable."""
    t0 = time.perf_counter()
    source = (source or os.environ.get("STATICDB_SOURCE", "local")).lower()
    if source not in ("local", "youtube"):
        raise ValueError(f"unknown source: {source!r}")

    loc = store.locate(record_id)
    if loc is None:
        raise RecordNotFound(record_id)
    seg = store.segment(loc.segment)
    geom = seg.geom
    p = geom.payload_per_frame

    first = loc.offset // p
    last = (loc.offset + loc.length - 1) // p
    wanted = range(first, last + 1)

    from_youtube = source == "youtube" and seg.youtube_id is not None
    served_from = "youtube" if from_youtube else ("local" if source == "local" else "local-fallback")

    try:
        if from_youtube:
            start = max(0.0, (first * geom.repeat) / seg.fps - YOUTUBE_PAD)
            end = ((last + 1) * geom.repeat) / seg.fps + YOUTUBE_PAD
            with tempfile.TemporaryDirectory(prefix="staticdb-") as tmp:
                section = download_section(seg.youtube_id, start, end, tmp)
                report = decode_frames(read_video(section, geom), geom, wanted)
        else:
            path = store.video_path(seg)
            if not path.exists():
                raise RecordUnavailable(f"segment file missing: {path}")
            start = max(0.0, (first * geom.repeat) / seg.fps - LOCAL_PAD)
            duration = ((last + 1 - first) * geom.repeat) / seg.fps + 2 * LOCAL_PAD
            report = decode_frames(read_video(path, geom, start, duration), geom, wanted)
    except RuntimeError as exc:  # ffmpeg trouble; RecordUnavailable is a RuntimeError too
        if isinstance(exc, RecordUnavailable):
            raise
        raise RecordUnavailable(str(exc)) from exc

    missing = [n for n in wanted if n not in report.payloads]
    if missing:
        hint = f"; first error: {report.errors[0]}" if report.errors else ""
        raise RecordUnavailable(
            f"frame(s) {missing} missing or corrupt ({report.scanned} scanned, "
            f"{len(report.errors)} bad){hint}"
        )

    blob = b"".join(report.payloads[n] for n in wanted)
    rel = loc.offset - first * p
    try:
        record = unpack_record(blob[rel : rel + loc.length])
    except ValueError as exc:
        raise RecordUnavailable(f"record bytes did not parse: {exc}") from exc

    meta = {
        "segment": seg.name,
        "source": served_from,
        "frames": [first, last],
        "frames_scanned": report.scanned,
        "rs_corrected_bytes": report.corrected,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return record, meta
