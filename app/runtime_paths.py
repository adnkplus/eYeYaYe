"""Resolve media tools from the project-contained runtime bundle.

The application never assumes MediaMTX or FFmpeg is installed in a host-wide
/usr/local/bin or /usr/bin location. System paths are retained only as a
compatibility fallback when a user explicitly configures a custom executable.
"""
from __future__ import annotations
import os
from pathlib import Path


def project_root() -> Path:
    configured = os.environ.get('CCTV_PROJECT_ROOT')
    if configured:
        return Path(configured).resolve()
    if Path('/project').is_dir():
        return Path('/project').resolve()
    return Path(__file__).resolve().parents[1]


ROOT = project_root()
CAM3_BUNDLE = ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE'
BIN_DIR = CAM3_BUNDLE / 'bin'


def bundled_binary(name: str) -> Path:
    if name not in {'mediamtx', 'ffmpeg', 'ffprobe'}:
        raise ValueError(f'Unsupported bundled binary: {name}')
    return BIN_DIR / name


def resolve_media_tools(values: dict) -> dict:
    """Replace legacy distro defaults with project-local binaries when present.

    An existing custom path is honoured. The old /usr/bin and /usr/local/bin
    defaults are treated as defaults, not as a requirement on the host.
    """
    resolved = dict(values)
    for setting, name in (('FFMPEG_BINARY', 'ffmpeg'), ('FFPROBE_BINARY', 'ffprobe')):
        configured = str(resolved.get(setting, f'/usr/bin/{name}') or '').strip()
        bundled = bundled_binary(name)
        old_defaults = {'', name, f'/usr/bin/{name}', f'/usr/local/bin/{name}'}
        if bundled.is_file() and (configured in old_defaults or not Path(configured).is_file()):
            resolved[setting] = str(bundled)
        elif configured:
            resolved[setting] = configured
        else:
            resolved[setting] = str(bundled)
    return resolved
