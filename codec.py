"""codec.py - bytes <-> black/white "TV static" frames.

Frame layout (default: 1920x1080 frame, 8x8-pixel blocks):

    240 x 135 blocks = 32,400 bits = 4,050 raw bytes per frame
    = 15 Reed-Solomon codewords (255 bytes each: 223 data + 32 parity),
      byte-interleaved so a burst of damaged blocks is spread over all codewords,
    + 225 spare bytes (zero padding).

The data part of every frame starts with a 10-byte header (frame number, payload
length, CRC32 of the payload). A frame therefore identifies itself, no matter
where a video seek or a YouTube section download happens to land.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Iterator, Optional

import cv2
import numpy as np
from reedsolo import ReedSolomonError, RSCodec

HEADER = struct.Struct(">IHI")  # frame_no, payload_len, crc32(payload)
CODEWORD = 255  # GF(256) Reed-Solomon codeword length


class FrameError(Exception):
    """A frame could not be decoded (too much damage, or the CRC did not match)."""


@dataclass(frozen=True)
class Geometry:
    width: int = 1920
    height: int = 1080
    block: int = 8  # each bit is a block x block square of pixels
    nsym: int = 32  # Reed-Solomon parity bytes per 255-byte codeword

    def __post_init__(self) -> None:
        if self.width % self.block or self.height % self.block:
            raise ValueError("width and height must be multiples of block")
        if not 2 <= self.nsym < CODEWORD - HEADER.size - 1:
            raise ValueError("nsym out of range")
        if self.codewords < 1:
            raise ValueError("frame too small to hold one Reed-Solomon codeword")

    @property
    def cols(self) -> int:
        return self.width // self.block

    @property
    def rows(self) -> int:
        return self.height // self.block

    @property
    def raw_bytes(self) -> int:
        return (self.cols * self.rows) // 8

    @property
    def codewords(self) -> int:
        return self.raw_bytes // CODEWORD

    @property
    def data_per_codeword(self) -> int:
        return CODEWORD - self.nsym

    @property
    def payload_per_frame(self) -> int:
        return self.codewords * self.data_per_codeword - HEADER.size


@lru_cache(maxsize=None)
def _rs(nsym: int) -> RSCodec:
    return RSCodec(nsym)


# --------------------------------------------------------------------------- encode


def encode_frame(payload: bytes, frame_no: int, geom: Geometry = Geometry()) -> np.ndarray:
    """Turn up to ``geom.payload_per_frame`` bytes into one grayscale (0/255) frame."""
    if len(payload) > geom.payload_per_frame:
        raise ValueError(f"payload too large: {len(payload)} > {geom.payload_per_frame}")
    header = HEADER.pack(frame_no, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    k = geom.data_per_codeword
    data = (header + payload).ljust(geom.codewords * k, b"\0")

    rs = _rs(geom.nsym)
    words = b"".join(bytes(rs.encode(data[i * k : (i + 1) * k])) for i in range(geom.codewords))

    # Interleave: byte j of every codeword is stored together.
    grid = np.frombuffer(words, np.uint8).reshape(geom.codewords, CODEWORD)
    raw = grid.T.reshape(-1).tobytes().ljust(geom.raw_bytes, b"\0")

    bits = np.unpackbits(np.frombuffer(raw, np.uint8))
    bits = np.pad(bits, (0, geom.cols * geom.rows - bits.size))
    small = (bits.reshape(geom.rows, geom.cols) * 255).astype(np.uint8)
    return np.repeat(np.repeat(small, geom.block, axis=0), geom.block, axis=1)


def encode_blob(blob: bytes, geom: Geometry = Geometry(), start_frame: int = 0) -> Iterator[np.ndarray]:
    """Yield the frames that carry ``blob`` (always at least one frame)."""
    p = geom.payload_per_frame
    for i in range(frame_count(len(blob), geom)):
        yield encode_frame(blob[i * p : (i + 1) * p], start_frame + i, geom)


def frame_count(nbytes: int, geom: Geometry = Geometry()) -> int:
    return max(1, -(-nbytes // geom.payload_per_frame))


# --------------------------------------------------------------------------- decode


@dataclass
class DecodedFrame:
    frame_no: int
    payload: bytes
    corrected: int  # number of byte errors Reed-Solomon repaired


def image_to_raw(img: np.ndarray, geom: Geometry = Geometry()) -> bytes:
    """Frame image (any resolution) -> raw interleaved bytes."""
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.shape != (geom.rows, geom.cols):
        # Averaging each block's pixels is what makes 8x8 blocks survive compression.
        img = cv2.resize(img, (geom.cols, geom.rows), interpolation=cv2.INTER_AREA)
    bits = (img >= 128).astype(np.uint8).reshape(-1)
    return np.packbits(bits[: geom.raw_bytes * 8]).tobytes()


def _decode_codeword(word: bytes, geom: Geometry) -> tuple[bytes, int]:
    try:
        msg, _, errata = _rs(geom.nsym).decode(word)
    except ReedSolomonError as exc:
        raise FrameError(f"Reed-Solomon failed: {exc}") from exc
    return bytes(msg), len(errata)


def decode_frame(
    img: np.ndarray, geom: Geometry = Geometry(), wanted: Optional[range] = None
) -> Optional[DecodedFrame]:
    """Decode one frame image.

    Only the first codeword (which holds the header) is decoded to learn the frame
    number. If ``wanted`` is given and the frame is outside it, ``None`` is returned
    without paying for the other codewords.
    """
    raw = image_to_raw(img, geom)
    n = geom.codewords
    words = np.frombuffer(raw[: n * CODEWORD], np.uint8).reshape(CODEWORD, n).T

    msg0, corrected = _decode_codeword(bytes(words[0]), geom)
    frame_no, length, crc = HEADER.unpack_from(msg0)
    if wanted is not None and frame_no not in wanted:
        return None

    chunks = [msg0]
    for w in words[1:]:
        msg, fixed = _decode_codeword(bytes(w), geom)
        chunks.append(msg)
        corrected += fixed

    if length > geom.payload_per_frame:
        raise FrameError(f"frame {frame_no}: impossible payload length {length}")
    payload = b"".join(chunks)[HEADER.size : HEADER.size + length]
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise FrameError(f"frame {frame_no}: CRC mismatch")
    return DecodedFrame(frame_no, payload, corrected)


@dataclass
class DecodeReport:
    payloads: dict[int, bytes] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    corrected: int = 0  # total byte errors repaired by Reed-Solomon
    scanned: int = 0  # frame images looked at


def decode_frames(
    images: Iterable[np.ndarray], geom: Geometry = Geometry(), wanted: Optional[range] = None
) -> DecodeReport:
    """Decode many frames. Bad frames are recorded in ``errors`` instead of raising.

    With ``wanted``, decoding stops as soon as every wanted frame has been found.
    """
    report = DecodeReport()
    for img in images:
        report.scanned += 1
        try:
            frame = decode_frame(img, geom, wanted)
        except FrameError as exc:
            report.errors.append(f"image #{report.scanned - 1}: {exc}")
            continue
        if frame is None:
            continue
        report.payloads[frame.frame_no] = frame.payload
        report.corrected += frame.corrected
        if wanted is not None and all(n in report.payloads for n in wanted):
            break
    return report


def decode_blob(images: Iterable[np.ndarray], geom: Geometry = Geometry()) -> bytes:
    """Decode every frame and return the original blob (frames may arrive in any order)."""
    report = decode_frames(images, geom)
    if report.errors:
        raise FrameError(f"{len(report.errors)} frame(s) failed: {report.errors[0]}")
    last = max(report.payloads, default=-1)
    missing = [n for n in range(last + 1) if n not in report.payloads]
    if missing:
        raise FrameError(f"missing frames: {missing[:5]}")
    return b"".join(report.payloads[n] for n in range(last + 1))
