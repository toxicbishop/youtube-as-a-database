"""api.py - FastAPI front end.  Run: uvicorn api:app --reload

Env vars:
    STATICDB_DIR          data directory (default ./data)
    STATICDB_SOURCE       local | youtube (default local)
    STATICDB_MEME_DELAY   extra seconds to sleep per read, for comedic timing (default 0)
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from reader import RecordNotFound, RecordUnavailable, fetch_record
from store import Store

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="YouTubeDB",
    version="0.1.0",
    description="A database that stores its rows as TV static on YouTube. Fake data only.",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/customer/{customer_id}")
def get_customer(customer_id: int = PathParam(..., ge=1)) -> dict[str, Any]:
    """Fetch one record by decoding it back out of the video."""
    try:
        record, meta = fetch_record(get_store(), customer_id)
    except RecordNotFound:
        raise HTTPException(404, f"customer {customer_id} is not in the index") from None
    except RecordUnavailable as exc:
        raise HTTPException(502, f"the static was unreadable: {exc}") from exc

    delay = float(os.environ.get("STATICDB_MEME_DELAY", "0") or 0)
    if delay > 0:
        time.sleep(delay)
        meta["meme_delay_s"] = delay
    return {"customer": record, "meta": meta}


@app.put("/customer/{customer_id}", status_code=202)
def update_customer(
    customer_id: int = PathParam(..., ge=1), body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Queue a full replacement of the record. It becomes readable after POST /flush."""
    try:
        pending = get_store().queue_update(customer_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "status": "queued",
        "id": customer_id,
        "pending": pending,
        "consistency": "eventual (hours)",
        "note": "reads keep returning the old value until the next flush",
    }


@app.post("/flush")
def flush() -> dict[str, Any]:
    """Encode all queued updates into a new video and repoint the index."""
    store = get_store()
    segment = store.flush()
    if segment is None:
        return {"status": "nothing pending"}
    return {
        "status": "flushed",
        "segment": segment.name,
        "frames": segment.frames,
        "file": str(store.video_path(segment)),
        "next_step": (
            f"upload that file to YouTube as Unlisted, then: "
            f"python store.py register --segment {segment.name} --video-id <id>"
        ),
    }


@app.get("/stats")
def stats() -> dict[str, Any]:
    return get_store().stats()
