#!/usr/bin/env python3
"""Helpers for resolving capture devices across platforms."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _list_windows_devices() -> list[str]:
    try:
        from pygrabber.dshow_graph import FilterGraph
    except Exception as exc:
        raise RuntimeError(
            "DirectShow device listing requires pygrabber. "
            "Install dependencies with 'pip install -r requirements.txt'."
        ) from exc
    graph = FilterGraph()
    return graph.get_input_devices()


def _list_v4l_devices() -> list[str]:
    by_id_dir = Path("/dev/v4l/by-id")
    if by_id_dir.is_dir():
        return sorted(str(entry.resolve()) for entry in by_id_dir.iterdir())
    return sorted(str(path) for path in Path("/dev").glob("video*"))


def list_devices() -> list[str]:
    if is_windows():
        return _list_windows_devices()
    return _list_v4l_devices()


def _find_device_index(name: str, devices: Sequence[str]) -> int | None:
    name_lower = name.lower()
    for idx, device_name in enumerate(devices):
        if name_lower in device_name.lower():
            return idx
    return None


def resolve_device(
    device_arg: str | None, preferred_name: str
) -> tuple[int | str, list[str]]:
    if is_windows():
        devices: list[str] = []
        list_error: str | None = None
        try:
            devices = _list_windows_devices()
        except RuntimeError as exc:
            list_error = str(exc)

        if device_arg is None:
            idx = _find_device_index(preferred_name, devices) if devices else None
            if idx is not None:
                return idx, devices
            return 0, devices

        token = device_arg.strip()
        if token.isdigit():
            return int(token), devices

        if devices:
            idx = _find_device_index(token, devices)
            if idx is not None:
                return idx, devices

        error = f"Device '{device_arg}' was not found. Use --list-devices to see cameras."
        if list_error:
            error = f"{error}\n{list_error}"
        raise ValueError(error)

    devices = _list_v4l_devices()
    if device_arg:
        return device_arg, devices

    for path in devices:
        if preferred_name.lower() in Path(path).name.lower():
            return path, devices
    return (devices[0] if devices else 0), devices


def format_device_list(devices: Sequence[str]) -> str:
    if not devices:
        return "No capture devices found."
    lines = [f"[{idx}] {name}" for idx, name in enumerate(devices)]
    return "\n".join(lines)
