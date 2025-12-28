#!/usr/bin/env python3
"""
ScopeView: microscope camera viewer for the MikrOkularHD USB camera (or other capture devices).

On Windows, the script can match devices by DirectShow name. Use --device to
override the selected index or name substring when needed.
"""
from __future__ import annotations

import os
import sys

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")

import argparse
import time
from typing import Any

import cv2
import numpy as np

from microscope_capture import open_pygrabber_capture
from microscope_devices import format_device_list, list_devices, resolve_device

FrameType = Any


def normalize_fourcc(value: str | None) -> str | None:
    """Normalize FOURCC strings, allowing 'auto' to skip manual forcing."""
    if value is None:
        return None
    token = value.strip()
    if not token or token.lower() in {"auto", "default", "none"}:
        return None
    if len(token) != 4:
        raise ValueError("FOURCC codes must be exactly 4 characters.")
    return token.upper()


def describe_fourcc(raw_code: float) -> str:
    """Return the human-readable FOURCC from CAP_PROP_FOURCC values."""
    code = int(raw_code)
    if code == 0:
        return "UNKNOWN"
    chars = [chr((code >> (8 * i)) & 0xFF) for i in range(4)]
    return "".join(chars)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Display the live feed from the MikrOkularHD USB camera."
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
        "--window-title",
        default="ScopeView Live",
        help="Title for the preview window.",
    )
    parser.add_argument(
        "--window-width",
        type=int,
        default=1920,
        help="Initial preview window width in pixels (default: 1920).",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=1080,
        help="Initial preview window height in pixels (default: 1080).",
    )
    parser.add_argument(
        "--window-x",
        type=int,
        default=50,
        help="Top-left X coordinate of the preview window (default: 50).",
    )
    parser.add_argument(
        "--window-y",
        type=int,
        default=50,
        help="Top-left Y coordinate of the preview window (default: 50).",
    )
    parser.add_argument(
        "--display-backend",
        choices=("opencv", "pygame"),
        default="pygame",
        help="Select the display backend (default: pygame).",
    )
    parser.add_argument(
        "--capture-backend",
        choices=("auto", "dshow", "msmf", "v4l2", "pygrabber"),
        default="auto",
        help="Capture backend (default: auto). Use dshow/msmf/pygrabber on Windows, v4l2 on Linux.",
    )
    parser.add_argument(
        "--fourcc",
        default="MJPG",
        help="Preferred pixel format (e.g., MJPG, YUYV). Use 'auto' to leave it up to the driver.",
    )
    parser.add_argument(
        "--fallback-fourcc",
        default="YUYV",
        help="Fallback pixel format if the preferred one fails. Use 'auto' to disable.",
    )
    parser.add_argument(
        "--buffer-count",
        type=int,
        help="Request a specific driver buffer queue size.",
    )
    parser.add_argument(
        "--probe-frames",
        type=int,
        default=5,
        help="How many frames to probe when validating a capture (default: 5).",
    )
    parser.add_argument(
        "--max-empty",
        type=int,
        default=60,
        help="Consecutive failed frame reads before reconnecting (default: 60).",
    )
    parser.add_argument(
        "--max-reconnects",
        type=int,
        default=5,
        help="Maximum number of automatic reconnect attempts (default: 5).",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=1.0,
        help="Seconds to wait before a reconnect attempt (default: 1.0).",
    )
    parser.add_argument(
        "--no-retry",
        dest="auto_retry",
        action="store_false",
        help="Disable automatic reconnect attempts.",
    )
    parser.set_defaults(auto_retry=True)

    args = parser.parse_args()

    try:
        args.fourcc = normalize_fourcc(args.fourcc)
        args.fallback_fourcc = normalize_fourcc(args.fallback_fourcc)
    except ValueError as exc:
        parser.error(str(exc))

    return args


def configure_stream(
    capture: cv2.VideoCapture,
    args: argparse.Namespace,
    forced_fourcc: str | None,
) -> None:
    """Apply optional width/height/fps/fourcc settings to the capture device."""
    if forced_fourcc:
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*forced_fourcc))
    if args.buffer_count:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, args.buffer_count)
    if args.width:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    if args.height:
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if args.fps:
        capture.set(cv2.CAP_PROP_FPS, args.fps)


def report_stream_state(capture: cv2.VideoCapture) -> None:
    """Log the actual capture mode negotiated with the camera."""
    width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
    height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
    fps = capture.get(cv2.CAP_PROP_FPS)
    active_fourcc = describe_fourcc(capture.get(cv2.CAP_PROP_FOURCC))
    print(
        f"Active mode: {int(width)}x{int(height)} @ {fps:.2f} fps, format {active_fourcc}"
    )


def build_fourcc_candidates(args: argparse.Namespace) -> list[str | None]:
    candidates: list[str | None] = []
    if args.fourcc:
        candidates.append(args.fourcc)
    if args.fallback_fourcc and args.fallback_fourcc not in candidates:
        candidates.append(args.fallback_fourcc)
    candidates.append(None)  # driver default as a last resort
    return candidates


def prime_capture(
    capture: cv2.VideoCapture, probe_frames: int
) -> tuple[bool, FrameType | None]:
    """Read a few frames to ensure the stream is alive."""
    last_frame: FrameType | None = None
    for _ in range(max(0, probe_frames)):
        ok, frame = capture.read()
        if ok:
            return True, frame
        time.sleep(0.05)
    return False, last_frame


def acquire_capture(
    device: str | int,
    device_name: str | None,
    args: argparse.Namespace,
    fourcc_candidates: list[str | None],
    backend: int,
    backend_label: str,
) -> tuple[Any | None, str | None, FrameType | None]:
    """Try to open the capture device using the supplied FOURCC candidates."""
    for fourcc in fourcc_candidates:
        label = fourcc or "driver default"
        print(
            f"Attempting to open {device} using {backend_label} backend, "
            f"pixel format '{label}'..."
        )
        capture = cv2.VideoCapture(device, backend)
        if not capture.isOpened() and device_name:
            name_source = f"video={device_name}"
            print(f"  -> retrying by name: {name_source}")
            capture.release()
            capture = cv2.VideoCapture(name_source, backend)
        if not capture.isOpened():
            print("  -> unable to open device with this setting.")
            continue
        configure_stream(capture, args, fourcc)
        ok, first_frame = prime_capture(capture, args.probe_frames)
        if not ok:
            print("  -> stream produced no frames during probe, trying next option.")
            capture.release()
            continue
        report_stream_state(capture)
        return capture, fourcc, first_frame
    return None, None, None


def open_with_backends(
    device: str | int,
    device_name: str | None,
    args: argparse.Namespace,
    fourcc_candidates: list[str | None],
    backend_candidates: list[tuple[str, int | None]],
) -> tuple[
    Any | None,
    str | None,
    FrameType | None,
    str | None,
    int | None,
]:
    for backend_label, backend in backend_candidates:
        if backend_label == "pygrabber":
            print(f"Attempting to open {device} using pygrabber backend...")
            if not isinstance(device, int):
                print("  -> pygrabber requires a numeric device index.")
                continue
            capture, pending_frame = open_pygrabber_capture(device, args)
            if capture is not None:
                report_stream_state(capture)
                return capture, None, pending_frame, backend_label, backend
            continue

        if backend is None:
            continue

        capture, active_fourcc, pending_frame = acquire_capture(
            device,
            device_name,
            args,
            fourcc_candidates,
            backend,
            backend_label,
        )
        if capture is not None:
            return capture, active_fourcc, pending_frame, backend_label, backend
    return None, None, None, None, None


def prepare_display(
    args: argparse.Namespace, capture: cv2.VideoCapture
) -> dict[str, Any]:
    """Initialize the chosen display backend and return context data."""
    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or 640
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or 480
    ctx: dict[str, Any] = {"backend": args.display_backend, "size": (frame_width, frame_height)}

    if args.display_backend == "opencv":
        cv2.namedWindow(args.window_title, cv2.WINDOW_NORMAL)
        if args.window_width and args.window_height:
            cv2.resizeWindow(args.window_title, args.window_width, args.window_height)
        if args.window_x is not None and args.window_y is not None:
            cv2.moveWindow(args.window_title, args.window_x, args.window_y)
        placeholder = np.zeros(
            (
                max(1, args.window_height or frame_height or 1),
                max(1, args.window_width or frame_width or 1),
                3,
            ),
            dtype=np.uint8,
        )
        cv2.imshow(args.window_title, placeholder)
        cv2.waitKey(1)
        return ctx

    # pygame backend
    import pygame

    pygame.init()
    win_w = args.window_width or frame_width
    win_h = args.window_height or frame_height
    window = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption(args.window_title)
    ctx.update(
        {
            "pygame": pygame,
            "window": window,
            "display_size": (win_w, win_h),
        }
    )
    window.fill((0, 0, 0))
    pygame.display.flip()
    return ctx


def render_frame(
    ctx: dict[str, Any],
    args: argparse.Namespace,
    frame: FrameType,
) -> bool:
    """Render a frame using the selected backend. Return False to exit."""
    backend = ctx["backend"]

    if backend == "opencv":
        cv2.imshow(args.window_title, frame)
        try:
            if cv2.getWindowProperty(args.window_title, cv2.WND_PROP_VISIBLE) < 1:
                return False
        except cv2.error:
            return False
        key = cv2.waitKey(1) & 0xFF
        return key not in (27, ord("q"))

    pygame = ctx["pygame"]
    window = ctx["window"]
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
            return False

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    surface = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
    display_size = ctx["display_size"]
    if surface.get_size() != display_size:
        surface = pygame.transform.smoothscale(surface, display_size)
    window.blit(surface, (0, 0))
    pygame.display.flip()
    return True


def shutdown_display(ctx: dict[str, Any], args: argparse.Namespace) -> None:
    backend = ctx["backend"]
    if backend == "opencv":
        cv2.destroyAllWindows()
        return
    pygame = ctx["pygame"]
    pygame.quit()


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

    fourcc_candidates = build_fourcc_candidates(args)
    print(f"Opening camera device: {device}")
    capture, _active_fourcc, pending_frame, active_backend_label, active_backend = open_with_backends(
        device, device_name, args, fourcc_candidates, backend_candidates
    )

    if capture is None:
        print(
            "Failed to open the camera. "
            "Use --list-devices or --device to select a camera, "
            "and try --capture-backend (dshow/msmf) if needed.",
            file=sys.stderr,
        )
        return 1

    display_ctx = prepare_display(args, capture)
    print("Press 'q' or ESC to quit the viewer.")

    consecutive_failures = 0
    reconnects = 0

    try:
        while True:
            if pending_frame is not None:
                frame = pending_frame
                pending_frame = None
                ok = True
            else:
                ok, frame = capture.read()

            if not ok:
                consecutive_failures += 1
                if consecutive_failures < args.max_empty:
                    time.sleep(0.01)
                    continue

                if not args.auto_retry or reconnects >= args.max_reconnects:
                    print("No frame received from the camera.", file=sys.stderr)
                    break

                print("Lost camera signal, attempting to reopen...", file=sys.stderr)
                capture.release()
                time.sleep(args.retry_delay)
                reconnect_candidates = backend_candidates
                if active_backend_label and active_backend is not None:
                    reconnect_candidates = [(active_backend_label, active_backend)] + [
                        (label, backend)
                        for label, backend in backend_candidates
                        if (label, backend) != (active_backend_label, active_backend)
                    ]
                capture, _active_fourcc, pending_frame, active_backend_label, active_backend = open_with_backends(
                    device, device_name, args, fourcc_candidates, reconnect_candidates
                )
                if capture is None:
                    print(
                        "Unable to recover the camera stream after reconnect attempts.",
                        file=sys.stderr,
                    )
                    break
                consecutive_failures = 0
                reconnects += 1
                continue

            consecutive_failures = 0
            reconnects = 0
            if not render_frame(display_ctx, args, frame):
                break
    finally:
        capture.release()
        shutdown_display(display_ctx, args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
