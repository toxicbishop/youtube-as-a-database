import random

import pytest

from codec import Geometry, decode_blob, decode_frames, encode_blob, frame_count
from video import read_video, recompress, write_video

GEOM = Geometry()
FPS = 30


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    blob = random.Random(3).randbytes(150_000)  # 45 frames = 1.5 s of static
    path = tmp_path_factory.mktemp("video") / "static.mp4"
    frames = write_video(encode_blob(blob, GEOM), path, GEOM, fps=FPS)
    return blob, path, frames


def test_frame_count(encoded):
    blob, _, frames = encoded
    assert frames == frame_count(len(blob), GEOM) * GEOM.repeat == 45 * 3


def test_video_roundtrip(encoded):
    blob, path, _ = encoded
    assert decode_blob(read_video(path, GEOM), GEOM) == blob


# What YouTube does to an upload is lossy re-encoding, often at a different resolution.
@pytest.mark.parametrize("height,crf", [(1080, 28), (720, 30), (720, 36), (480, 36)])
def test_survives_recompression(encoded, tmp_path, height, crf):
    blob, path, _ = encoded
    out = tmp_path / f"re_{height}_{crf}.mp4"
    recompress(path, out, height=height, crf=crf)
    assert decode_blob(read_video(out, GEOM), GEOM) == blob


def test_section_read_finds_the_right_frames(encoded):
    blob, path, _ = encoded
    p = GEOM.payload_per_frame
    wanted = range(20, 23)
    start = (20 * GEOM.repeat) / FPS - 0.25
    duration = (3 * GEOM.repeat) / FPS + 0.5
    report = decode_frames(read_video(path, GEOM, start, duration), GEOM, wanted)
    assert not report.errors
    assert sorted(report.payloads) == [20, 21, 22]
    assert b"".join(report.payloads[n] for n in wanted) == blob[20 * p : 23 * p]
