#!/usr/bin/env python3
"""
Capture raw frames from the MikrOkularHD (or any capture device) without color
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

from microscope_capture import open_pygrabber_capture
from microscope_devices import format_device_list, list_devices, resolve_device


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump raw frames from a capture device (no color conversion)."
    )
    parser.add_argument(
        "--device",
        help="Device index or name substring (default: auto-detected).",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available capture devices and exit.",
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
        "--capture-backend",
        choices=("auto", "dshow", "msmf", "v4l2", "pygrabber"),
        default="auto",
        help="Capture backend (default: auto). Use dshow/msmf/pygrabber on Windows, v4l2 on Linux.",
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

    if args.list_devices:
        try:
            devices = list_devices()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(format_device_list(devices))
        return 0

    try:
        device, devices = resolve_device(args.device, "MikrOkularHD")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    backend_map: dict[str, int | None] = {
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
        "v4l2": cv2.CAP_V4L2,
        "any": cv2.CAP_ANY,
        "pygrabber": None,
    }
    if args.capture_backend == "auto":
        if sys.platform.startswith("win"):
            backend_candidates = [
                ("dshow", backend_map["dshow"]),
                ("msmf", backend_map["msmf"]),
                ("any", backend_map["any"]),
                ("pygrabber", backend_map["pygrabber"]),
            ]
        else:
            backend_candidates = [
                ("v4l2", backend_map["v4l2"]),
                ("any", backend_map["any"]),
            ]
    else:
        backend_candidates = [(args.capture_backend, backend_map[args.capture_backend])]

    device_name = None
    if sys.platform.startswith("win") and isinstance(device, int):
        if 0 <= device < len(devices):
            device_name = devices[device]

    cap = None
    pending_frame = None
    for backend_label, backend in backend_candidates:
        if backend_label == "pygrabber":
            if not isinstance(device, int):
                if not args.silent:
                    print("pygrabber requires a numeric device index.")
                continue
            if not args.silent:
                print("Attempting to open using pygrabber backend...")
            cap, pending_frame = open_pygrabber_capture(device, args)
            if cap is not None:
                break
            continue

        if backend is None:
            continue

        cap = cv2.VideoCapture(device, backend)
        if not cap.isOpened() and device_name:
            name_source = f"video={device_name}"
            if not args.silent:
                print(f"Retrying by name: {name_source}")
            cap.release()
            cap = cv2.VideoCapture(name_source, backend)
        if cap.isOpened():
            break
        if not args.silent:
            print(f"Unable to open device using {backend_label} backend.")

    if cap is None or not cap.isOpened():
        print(
            "Failed to open the camera. Use --list-devices or --device to select a camera, "
            "and try --capture-backend (dshow/msmf) if needed.",
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
            if pending_frame is not None:
                ok, frame = True, pending_frame
                pending_frame = None
            else:
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
