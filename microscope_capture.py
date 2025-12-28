#!/usr/bin/env python3
"""Capture helpers with an optional DirectShow (pygrabber) fallback."""
from __future__ import annotations

import sys
import threading
from typing import Any, Iterable

import cv2
import numpy as np


def _format_matches(fmt: dict[str, Any], args: Any) -> bool:
    if args.width and fmt.get("width") != args.width:
        return False
    if args.height and fmt.get("height") != args.height:
        return False
    if args.fps:
        min_fps = fmt.get("min_framerate") or 0
        max_fps = fmt.get("max_framerate") or 0
        if not (min_fps <= args.fps <= max_fps):
            return False
    fourcc_value = (args.fourcc or "").strip()
    if fourcc_value and fourcc_value.lower() not in {"auto", "default", "none"}:
        requested = fourcc_value.upper()
        allowed = {requested}
        if requested == "YUYV":
            allowed.add("YUY2")
        if fmt.get("media_type_str") not in allowed:
            return False
    return True


def _choose_format(formats: Iterable[dict[str, Any]], args: Any) -> dict[str, Any] | None:
    fourcc_value = (args.fourcc or "").strip()
    if not (
        args.width
        or args.height
        or args.fps
        or (fourcc_value and fourcc_value.lower() not in {"auto", "default", "none"})
    ):
        return None
    matches = [fmt for fmt in formats if _format_matches(fmt, args)]
    if not matches:
        return None
    if args.fps:
        matches.sort(
            key=lambda f: abs(
                args.fps
                - max(min(args.fps, f.get("max_framerate") or 0), f.get("min_framerate") or 0)
            )
        )
    return matches[0]


class PyGrabberCapture:
    def __init__(self, device_index: int, args: Any):
        if not sys.platform.startswith("win"):
            raise RuntimeError("pygrabber capture is only supported on Windows.")

        from comtypes import CoInitialize  # type: ignore
        from pygrabber.dshow_graph import FilterGraph  # type: ignore

        CoInitialize()

        self._graph = FilterGraph()
        self._frame_ready = threading.Event()
        self._frame: np.ndarray | None = None
        self._opened = False
        self._width = 0
        self._height = 0
        self._fps = 0.0

        def _on_frame(frame: np.ndarray) -> None:
            self._frame = frame
            self._frame_ready.set()

        self._graph.add_video_input_device(device_index)
        video_input = self._graph.get_input_device()
        selected_format = _choose_format(video_input.get_formats(), args)
        if selected_format is not None:
            video_input.set_format(selected_format["index"])
            if args.fps:
                self._fps = args.fps
            else:
                self._fps = float(selected_format.get("max_framerate") or 0)

        self._graph.add_sample_grabber(_on_frame)
        self._graph.add_null_render()
        self._graph.prepare_preview_graph()
        self._graph.run()
        self._opened = True

    def isOpened(self) -> bool:  # noqa: N802
        return self._opened

    def read(self, timeout: float = 1.0) -> tuple[bool, np.ndarray | None]:
        if not self._opened:
            return False, None
        self._frame_ready.clear()
        self._frame = None
        self._graph.grab_frame()
        if not self._frame_ready.wait(timeout):
            return False, None
        if self._frame is None:
            return False, None
        frame = cv2.cvtColor(self._frame, cv2.COLOR_RGB2BGR)
        self._height, self._width = frame.shape[:2]
        return True, frame

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        if prop_id == cv2.CAP_PROP_FPS:
            return float(self._fps)
        if prop_id == cv2.CAP_PROP_FOURCC:
            return 0.0
        return 0.0

    def release(self) -> None:
        if not self._opened:
            return
        self._opened = False
        try:
            self._graph.stop()
        finally:
            self._graph.remove_filters()


def open_pygrabber_capture(
    device_index: int,
    args: Any,
) -> tuple[PyGrabberCapture | None, np.ndarray | None]:
    if not sys.platform.startswith("win"):
        print("  -> pygrabber backend is only available on Windows.")
        return None, None
    try:
        capture = PyGrabberCapture(device_index, args)
    except Exception as exc:
        print(f"  -> pygrabber backend failed: {exc}")
        return None, None
    ok, frame = capture.read()
    if not ok:
        capture.release()
        print("  -> pygrabber backend produced no frames.")
        return None, None
    return capture, frame
