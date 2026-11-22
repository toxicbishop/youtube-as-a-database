"""calibrate.py - Test matrix video generation and recovery analysis.

Generates a calibration video with multiple parameter slices (block size,
repetition factor, Reed-Solomon parity) and analyzes which survive YouTube's
compression to scientifically tune production settings.

Usage:
    python calibrate.py generate [--out calibrate.mp4]
    python calibrate.py analyze --file data/videos/re_720_36.mp4
    python calibrate.py analyze --video-id <YOUTUBE_ID>
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from codec import (
    CODEWORD,
    Geometry,
    decode_frames,
    encode_blob,
    image_to_raw,
)
from reader import download_section
from video import read_video, write_video

CONFIGS = [
    {"name": "6x6-rep3-nsym32", "geom": Geometry(block=6, repeat=3, nsym=32), "duration_sec": 2.0},
    {"name": "8x8-rep1-nsym32", "geom": Geometry(block=8, repeat=1, nsym=32), "duration_sec": 2.0},
    {"name": "8x8-rep3-nsym16", "geom": Geometry(block=8, repeat=3, nsym=16), "duration_sec": 2.0},
    {"name": "8x8-rep3-nsym32", "geom": Geometry(block=8, repeat=3, nsym=32), "duration_sec": 2.0},
    {"name": "8x8-rep5-nsym32", "geom": Geometry(block=8, repeat=5, nsym=32), "duration_sec": 2.0},
    {"name": "10x10-rep3-nsym32", "geom": Geometry(block=10, repeat=3, nsym=32), "duration_sec": 2.0},
]


def generate_calibration_video(out_path: Path, manifest_path: Path, seed: int = 42, fps: int = 30) -> None:
    rng = random.Random(seed)
    all_frames = []
    manifest = []
    current_time = 0.0

    print("Generating calibration slices:")
    for cfg in CONFIGS:
        geom: Geometry = cfg["geom"]
        name: str = cfg["name"]
        # Calculate how many unique logical frames fit in duration
        unique_frames = max(2, int((cfg["duration_sec"] * fps) / geom.repeat))
        payload_bytes = unique_frames * geom.payload_per_frame
        blob = rng.randbytes(payload_bytes)

        # Generate frames
        encoded_unique = list(encode_blob(blob, geom))
        slice_frames = []
        for f in encoded_unique:
            for _ in range(geom.repeat):
                slice_frames.append(f)

        slice_duration = len(slice_frames) / fps
        manifest.append({
            "name": name,
            "geom": asdict(geom),
            "start_sec": current_time,
            "end_sec": current_time + slice_duration,
            "unique_frames": unique_frames,
            "total_frames": len(slice_frames),
            "payload_bytes": payload_bytes,
        })
        all_frames.extend(slice_frames)
        print(f"  • {name:20}: {len(slice_frames):>3} frames ({slice_duration:.1f}s) @ {current_time:.1f}s - {current_time + slice_duration:.1f}s")
        current_time += slice_duration

    # Write combined video (frames are already repeated per-slice)
    write_video(all_frames, out_path, Geometry(repeat=1), fps=fps, crf=18)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSaved calibration video: {out_path} ({out_path.stat().st_size / 1024:.0f} KiB, {len(all_frames)} frames)")
    print(f"Saved manifest: {manifest_path}")


def analyze_slice(video_path: Path, item: dict, fps: int = 30) -> dict:
    geom = Geometry(**item["geom"])
    start = max(0.0, item["start_sec"] - 0.2)
    duration = (item["end_sec"] - item["start_sec"]) + 0.4
    wanted = range(item["unique_frames"])

    images = list(read_video(video_path, geom, start=start, duration=duration))
    report = decode_frames(images, geom, wanted)

    recovered = len([n for n in wanted if n in report.payloads])
    total_wanted = len(wanted)
    success = recovered == total_wanted

    return {
        "name": item["name"],
        "block": geom.block,
        "repeat": geom.repeat,
        "nsym": geom.nsym,
        "scanned": report.scanned,
        "recovered": f"{recovered}/{total_wanted}",
        "rs_corrected_bytes": report.corrected,
        "survived": "✅ YES" if success else "❌ NO",
    }


def analyze(target_file: Optional[Path], video_id: Optional[str], manifest_path: Path) -> None:
    if not manifest_path.exists():
        print(f"Error: Manifest file {manifest_path} not found. Run generate first.")
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = []

    if video_id:
        with tempfile.TemporaryDirectory(prefix="calib-") as tmp:
            tmp_path = Path(tmp)
            for item in manifest:
                print(f"Downloading section for {item['name']} ({item['start_sec']:.1f}s - {item['end_sec']:.1f}s)...")
                sec_file = download_section(video_id, item["start_sec"], item["end_sec"], tmp_path)
                res = analyze_slice(sec_file, item)
                results.append(res)
    else:
        if not target_file or not target_file.exists():
            print(f"Error: Target video file {target_file} not found.")
            return
        for item in manifest:
            res = analyze_slice(target_file, item)
            results.append(res)

    print("\n" + "=" * 80)
    print("CALIBRATION ANALYSIS RESULTS")
    print("=" * 80)
    header = f"| {'Configuration':<20} | {'Block':<5} | {'Repeat':<6} | {'NSym':<4} | {'Recovered':<9} | {'RS Repairs':<10} | {'Status':<8} |"
    sep = "|-" + "-|-".join(["-" * 20, "-" * 5, "-" * 6, "-" * 4, "-" * 9, "-" * 10, "-" * 8]) + "-|"
    print(header)
    print(sep)
    for r in results:
        print(f"| {r['name']:<20} | {r['block']:<5} | {r['repeat']:<6} | {r['nsym']:<4} | {r['recovered']:<9} | {r['rs_corrected_bytes']:<10} | {r['survived']:<8} |")
    print("=" * 80)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="create calibration test video and manifest")
    gen.add_argument("--out", default="calibrate.mp4", help="output video path")
    gen.add_argument("--manifest", default="calibration_manifest.json", help="output manifest path")

    ana = sub.add_parser("analyze", help="analyze survival of slices from file or YouTube")
    ana.add_argument("--file", help="local video file to analyze")
    ana.add_argument("--video-id", help="YouTube video id to download and test")
    ana.add_argument("--manifest", default="calibration_manifest.json", help="manifest path")

    args = parser.parse_args()
    if args.cmd == "generate":
        generate_calibration_video(Path(args.out), Path(args.manifest))
    elif args.cmd == "analyze":
        analyze(Path(args.file) if args.file else None, args.video_id, Path(args.manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
