#!/usr/bin/env python3
"""
Capture raw frames from the MikrOkularHD (or any V4L2 device) without colour
conversion and dump them to disk/stdout along with optional metadata.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

import cv2


def find_device(preferred_name: str) -> str | int:
    by_id_dir = Path("/dev/v4l/by-id")
    if by_id_dir.is_dir():
        for entry in by_id_dir.iterdir():
            if preferred_name.lower() in entry.name.lower():
                return str(entry.resolve())
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump raw frames from a V4L2 device (no colour conversion)."
    )
    parser.add_argument(
        "--device",
        help="Path or index of the V4L2 device (default: auto-detected MikrOkularHD).",
    )
    parser.add_argument(
        "--width",
        type=int,
        help="Request a specific frame width (pixels).",
    )
    parser.add_argument(
        "--height",
        type=int,
        help="Request a specific frame height (pixels).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        help="Request a specific frame rate.",
    )
    parser.add_argument(
        "--fourcc",
        default="YUYV",
        help="Desired pixel format (e.g., YUYV, MJPG). Use 'auto' to skip forcing.",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=1,
        help="How many frames to capture (default: 1).",
    )
    parser.add_argument(
        "--output",
        default="frame.raw",
        help="Binary output file path (use '-' for stdout).",
    )
    parser.add_argument(
        "--metadata",
        help="Optional path to write JSON metadata about the capture.",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Suppress progress output (useful when piping raw data).",
    )
    return parser.parse_args()


def configure_capture(cap: cv2.VideoCapture, args: argparse.Namespace) -> None:
    if args.fourcc and args.fourcc.lower() != "auto":
        fourcc = args.fourcc.strip().upper()
        if len(fourcc) != 4:
            raise ValueError("FOURCC must be exactly 4 characters.")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
    if args.width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    if args.height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if args.fps:
        cap.set(cv2.CAP_PROP_FPS, args.fps)


@contextmanager
def output_stream(path: str) -> Iterator[BinaryIO]:
    if path == "-":
        yield sys.stdout.buffer
    else:
        with open(path, "wb") as handle:
            yield handle


def main() -> int:
    args = parse_args()

    device = args.device if args.device is not None else find_device("MikrOkularHD")
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(
            "Failed to open the camera. Use --device to specify the /dev/video* path.",
            file=sys.stderr,
        )
        return 1

    try:
        configure_capture(cap, args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    metadata = {
        "device": device,
        "requested": {
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "fourcc": None if args.fourcc.lower() == "auto" else args.fourcc.upper(),
        },
        "captured": {},
        "frames": [],
    }

    with output_stream(args.output) as out:
        for idx in range(args.frames):
            ok, frame = cap.read()
            if not ok:
                print("Failed to read frame from camera.", file=sys.stderr)
                return 3
            out.write(frame.tobytes())
            metadata["frames"].append(
                {
                    "index": idx,
                    "shape": frame.shape,
                    "dtype": str(frame.dtype),
                    "bytes": frame.nbytes,
                }
            )
            if not args.silent:
                print(
                    f"Captured frame {idx+1}/{args.frames}: shape={frame.shape}, dtype={frame.dtype}"
                )

    metadata["captured"] = {
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": cap.get(cv2.CAP_PROP_FPS),
        "fourcc": int(cap.get(cv2.CAP_PROP_FOURCC)),
        "convert_rgb": cap.get(cv2.CAP_PROP_CONVERT_RGB),
    }

    cap.release()

    if args.metadata:
        with open(args.metadata, "w", encoding="utf-8") as meta_handle:
            json.dump(metadata, meta_handle, indent=2)
        if not args.silent:
            print(f"Metadata written to {args.metadata}")

    if not args.silent and args.output != "-":
        size = Path(args.output).stat().st_size
        print(f"Wrote {size} bytes to {args.output}")

    # When piping to stdout we should flush and avoid closing sys.stdout.
    if args.output == "-":
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
