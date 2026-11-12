#!/usr/bin/env python3
"""Validated persistent desktop workspace settings for eYeYaYe."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

DEFAULT_WORKSPACE = {
    "schema_version": 1,
    "window_geometry": "1500x920",
    "page": "Live & Playback",
    "show_live": True,
    "show_playback": True,
    "live_camera_enabled": {},
    "split_orientation": "side-by-side",
    "sash_position": 0,
    "live_grid": {"columns": 3, "rows": 1},
    "playback_grid": {"columns": 3, "rows": 1},
}
VALID_PAGES = {
    "Live & Playback", "Cameras", "Cue Library", "Export Studio", "Archives",
    "Detection Zones", "Appearance", "Commands", "Settings", "Logs", "Maintenance",
}
_GEOMETRY = re.compile(r"^\d{3,5}x\d{3,5}(?:[+-]\d{1,5}){0,2}$")


def normalize_workspace(raw: object) -> dict:
    """Return a complete, safe workspace state; ignore malformed fields."""
    state = json.loads(json.dumps(DEFAULT_WORKSPACE))
    if not isinstance(raw, dict):
        return state
    geometry = raw.get("window_geometry")
    if isinstance(geometry, str) and _GEOMETRY.fullmatch(geometry):
        try:
            w, h = (int(x) for x in geometry.split("+", 1)[0].split("x"))
            if 900 <= w <= 12000 and 600 <= h <= 8000:
                state["window_geometry"] = geometry
        except Exception:
            pass
    page = raw.get("page")
    if page in VALID_PAGES:
        state["page"] = page
    for key in ("show_live", "show_playback"):
        if isinstance(raw.get(key), bool):
            state[key] = raw[key]
    live_camera_enabled = raw.get("live_camera_enabled")
    if isinstance(live_camera_enabled, dict):
        state["live_camera_enabled"] = {
            str(camera_id): enabled for camera_id, enabled in live_camera_enabled.items()
            if isinstance(camera_id, str) and camera_id and len(camera_id) <= 64
            and isinstance(enabled, bool)
        }
    if raw.get("split_orientation") in ("side-by-side", "stacked"):
        state["split_orientation"] = raw["split_orientation"]
    try:
        sash = int(raw.get("sash_position", 0))
        if 0 <= sash <= 12000:
            state["sash_position"] = sash
    except (TypeError, ValueError):
        pass
    for key in ("live_grid", "playback_grid"):
        grid = raw.get(key)
        if isinstance(grid, dict):
            clean = {}
            for dimension in ("columns", "rows"):
                try:
                    value = int(grid.get(dimension, state[key][dimension]))
                except (TypeError, ValueError):
                    value = state[key][dimension]
                clean[dimension] = max(1, min(32, value))
            if clean["columns"] * clean["rows"] > 64:
                clean["rows"] = max(1, 64 // clean["columns"])
            state[key] = clean
    return state


def load_workspace(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    return normalize_workspace(raw)


def save_workspace(path: Path, state: dict) -> dict:
    clean = normalize_workspace(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return clean
