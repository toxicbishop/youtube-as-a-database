import random

import numpy as np
import pytest

from codec import (
    FrameError,
    Geometry,
    decode_blob,
    decode_frame,
    decode_frames,
    encode_blob,
    encode_frame,
)

GEOM = Geometry()
rng = random.Random(7)


def flip_blocks(img: np.ndarray, fraction: float, seed: int = 0) -> np.ndarray:
    """Invert a random fraction of the 8x8 blocks (a stand-in for compression damage)."""
    mask = np.random.default_rng(seed).random((GEOM.rows, GEOM.cols)) < fraction
    big = np.repeat(np.repeat(mask, GEOM.block, 0), GEOM.block, 1)
    out = img.copy()
    out[big] = 255 - out[big]
    return out


def test_geometry_defaults():
    assert (GEOM.cols, GEOM.rows) == (240, 135)
    assert GEOM.raw_bytes == 4050
    assert GEOM.codewords == 15
    assert GEOM.payload_per_frame == 3335
    assert GEOM.repeat == 3


def test_frame_roundtrip():
    payload = rng.randbytes(GEOM.payload_per_frame)
    img = encode_frame(payload, 7)
    assert img.shape == (1080, 1920)
    assert set(np.unique(img)) <= {0, 255}
    frame = decode_frame(img)
    assert (frame.frame_no, frame.payload, frame.corrected) == (7, payload, 0)


@pytest.mark.parametrize("size", [0, 1, 500])
def test_short_payloads(size):
    payload = rng.randbytes(size)
    assert decode_frame(encode_frame(payload, 3)).payload == payload


def test_payload_too_large():
    with pytest.raises(ValueError):
        encode_frame(b"x" * (GEOM.payload_per_frame + 1), 0)


def test_other_geometry():
    g = Geometry(width=640, height=360, block=4, nsym=16)
    payload = rng.randbytes(g.payload_per_frame)
    assert decode_frame(encode_frame(payload, 1, g), g).payload == payload


def test_reed_solomon_repairs_scattered_errors():
    payload = rng.randbytes(GEOM.payload_per_frame)
    damaged = flip_blocks(encode_frame(payload, 0), fraction=0.003)
    frame = decode_frame(damaged)
    assert frame.payload == payload
    assert frame.corrected > 0


def test_interleaving_repairs_a_burst():
    payload = rng.randbytes(GEOM.payload_per_frame)
    img = encode_frame(payload, 0)
    img[80:104, :] = 255 - img[80:104, :]  # three whole rows of blocks inverted
    frame = decode_frame(img)
    assert frame.payload == payload
    assert frame.corrected >= 80


def test_heavy_damage_is_detected_not_silently_wrong():
    img = flip_blocks(encode_frame(rng.randbytes(GEOM.payload_per_frame), 0), fraction=0.4)
    with pytest.raises(FrameError):
        decode_frame(img)


def test_wanted_filter_skips_other_frames():
    img = encode_frame(b"hello", 9)
    assert decode_frame(img, wanted=range(0, 3)) is None
    assert decode_frame(img, wanted=range(9, 10)).payload == b"hello"


def test_blob_roundtrip_any_order():
    blob = rng.randbytes(20_000)
    frames = list(encode_blob(blob))
    assert len(frames) == 6  # ceil(20000 / 3335)
    rng.shuffle(frames)
    assert decode_blob(frames) == blob


def test_decode_blob_reports_missing_frame():
    frames = list(encode_blob(rng.randbytes(20_000)))
    del frames[1]
    with pytest.raises(FrameError, match="missing"):
        decode_blob(frames)


def test_decode_frames_stops_early_when_done():
    frames = encode_blob(rng.randbytes(20_000))
    report = decode_frames(frames, wanted=range(1, 3))
    assert sorted(report.payloads) == [1, 2]
    assert report.scanned == 3  # frame 0 skipped cheaply, 1 and 2 decoded, then stop
