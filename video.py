"""video.py - frames <-> mp4 via FFmpeg (raw frames are piped, no temp images)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

from codec import Geometry


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        candidate = Path(sys.executable).parent / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if candidate.is_file():
            exe = str(candidate)
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH (https://ffmpeg.org/download.html)")
    return exe


def write_video(
    frames: Iterable[np.ndarray],
    path: str | Path,
    geom: Geometry = Geometry(),
    fps: int = 30,
    crf: int = 18,
    preset: str = "veryfast",
) -> int:
    """Encode grayscale frames to an H.264 mp4. Returns the number of frames written."""
    cmd = [
        _ffmpeg(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{geom.width}x{geom.height}", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-g", str(fps),  # a keyframe every second keeps section reads cheap
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(path),
    ]  # fmt: skip
    count = 0
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for frame in frames:
                proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
                count += 1
        except BrokenPipeError:
            pass  # ffmpeg died; its stderr explains why (below)
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            raise RuntimeError(f"ffmpeg failed ({rc}): {err.read().decode(errors='replace')}")
    return count


def read_video(
    path: str | Path,
    geom: Geometry = Geometry(),
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> Iterator[np.ndarray]:
    """Yield frames (already averaged down to the bit grid) from a video or a section of it.

    The video may have been re-encoded at any resolution (YouTube does that);
    ffmpeg's area scaler averages every block back to a single value.
    """
    cmd = [_ffmpeg(), "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vf", f"scale={geom.cols}:{geom.rows}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]  # fmt: skip
    size = geom.cols * geom.rows
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        got = 0
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                got += 1
                yield np.frombuffer(buf, np.uint8).reshape(geom.rows, geom.cols)
        finally:
            proc.kill()
            proc.wait()
        if got == 0:
            err.seek(0)
            raise RuntimeError(f"ffmpeg produced no frames: {err.read().decode(errors='replace')}")


def recompress(src: str | Path, dst: str | Path, height: int = 720, crf: int = 30) -> None:
    """Re-encode a video lossily, to simulate what YouTube does to an upload."""
    cmd = [
        _ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        str(dst),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
