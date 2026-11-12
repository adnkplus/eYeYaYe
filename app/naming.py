"""Stable recording filenames for eYeYaYe.

New output order (time first, as requested):
    HHMMSS_DDMMYYYY_TYPE_C1.ext
    HHMMSS_DDMMYYYY_MER_C1_P01.mp4

MediaMTX supports the four-digit %Y token, not %y, so the year is written in
full. Parsers retain support for existing V3/V4/V6 and legacy filenames.
"""
from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime

KINDS = ("RAW", "MER", "TLR", "TLI")

# Current time-first format: HHMMSS_DDMMYYYY_TYPE_C1[_P01[_generation]]
_TIME_FIRST_FULL = re.compile(
    r"^(?P<clock>\d{6})_(?P<date>\d{8})_"
    r"(?P<kind>RAW|MER|TLR|TLI)_(?P<camera>C[0-9A-Z]+)"
    r"(?:_P(?P<part>\d+)(?:_(?P<generation>\d+))?)?$"
)
# Short-year time-first format is accepted for user-renamed/older generated files.
_TIME_FIRST_SHORT = re.compile(
    r"^(?P<clock>\d{6})_(?P<date>\d{6})_"
    r"(?P<kind>RAW|MER|TLR|TLI)_(?P<camera>C[0-9A-Z]+)"
    r"(?:_P(?P<part>\d+)(?:_(?P<generation>\d+))?)?$"
)

# Previous V6 generated format: C1_HHMMSS_DDMM_KIND_YY[_P01[_generation]]
_SHORT_YEAR = re.compile(
    r"^(?P<camera>C[0-9A-Z]+)_(?P<clock>\d{6})_(?P<date>\d{4})_"
    r"(?P<kind>RAW|MER|TLR|TLI)_(?P<year>\d{2})"
    r"(?:_P(?P<part>\d+)(?:_(?P<generation>\d+))?)?$"
)
# Previous V6 RAW format with year and offset stored in parent directories.
_RAW_YEARLESS = re.compile(
    r"^(?P<camera>C[0-9A-Z]+)_(?P<clock>\d{6})_(?P<date>\d{4})_RAW$"
)
# V4 historical format.
_FULL_YEAR = re.compile(
    r"^(?P<camera>C[0-9A-Z]+)_(?P<clock>\d{6})_(?P<date>\d{4})_"
    r"(?P<kind>RAW|MER|TLR|TLI)_(?P<year>\d{4})(?:_(?P<zone>[+-]\d{4}))?"
    r"(?:_P(?P<part>\d+)(?:_(?P<generation>\d+))?)?$"
)
# V3 intermediate names (DDMMYY before media type).
_INTERMEDIATE = re.compile(
    r"^(?P<camera>C[0-9A-Z]+)_(?P<clock>\d{6})_(?P<date>\d{6})_"
    r"(?P<kind>RAW|MER|TLR|TLI)(?:_P(?P<part>\d+)(?:_(?P<generation>\d+))?)?$"
)
# Older V3 names (type before time).
_LEGACY = re.compile(
    r"^(?P<camera>C[0-9A-Z]+)_(?P<kind>RAW|MER|TLR|TLI)_"
    r"(?P<clock>\d{6})_(?P<date>\d{6})(?:_P(?P<part>\d+)(?:_\d+)?)?$"
)


def filename(camera_code: str, when: datetime, kind: str, extension: str,
             part: int | None = None, generation: str = "") -> str:
    code = str(camera_code).upper()
    if not re.fullmatch(r"C[0-9A-Z]+", code):
        raise ValueError(f"Invalid camera filename code: {camera_code!r}")
    kind = str(kind).upper()
    if kind not in KINDS:
        raise ValueError(f"Invalid recording type: {kind!r}")
    ext = str(extension).lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        raise ValueError(f"Invalid file extension: {extension!r}")
    local_when = when if when.tzinfo is not None else when.astimezone()
    stem = f"{local_when.strftime('%H%M%S_%d%m%Y')}_{kind}_{code}"
    if part is not None:
        if int(part) < 1:
            raise ValueError("Part numbers start at 1.")
        stem += f"_P{int(part):02d}"
    if generation:
        if not str(generation).isdigit():
            raise ValueError("Generation suffix must be numeric.")
        stem += f"_{generation}"
    return f"{stem}.{ext}"


def _parse_datetime(clock: str, day_month: str, year_text: str, zone: str | None = None):
    text = f"{clock}_{day_month}_{year_text}"
    fmt = "%H%M%S_%d%m_%Y" if len(year_text) == 4 else "%H%M%S_%d%m_%y"
    if zone:
        text += f"_{zone}"
        fmt += "_%z"
    return datetime.strptime(text, fmt).astimezone()


def _parse_time_first(result: dict, year_digits: int, timezone_hint: str | None):
    date_text = result["date"]
    if year_digits == 4:
        day_month, year_text = date_text[:4], date_text[4:]
        zone = timezone_hint if timezone_hint and re.fullmatch(r"[+-]\d{4}", timezone_hint) else None
        result["started"] = _parse_datetime(result["clock"], day_month, year_text, zone)
        result["year"] = year_text
        result["zone"] = zone or ""
        return
    day_month, year_text = date_text[:4], date_text[4:]
    result["started"] = _parse_datetime(result["clock"], day_month, year_text)
    result["year"] = str(result["started"].year)
    result["zone"] = ""


def parse(stem: str, *, year_hint: int | str | None = None,
          timezone_hint: str | None = None) -> dict | None:
    """Parse the new time-first names and all supported historical names."""
    value = str(stem)
    for pattern, format_name in (
        (_TIME_FIRST_FULL, "time-first-full"),
        (_TIME_FIRST_SHORT, "time-first-short"),
        (_SHORT_YEAR, "short-year"),
        (_RAW_YEARLESS, "raw-yearless"),
        (_FULL_YEAR, "full-year"),
        (_INTERMEDIATE, "intermediate"),
        (_LEGACY, "legacy"),
    ):
        match = pattern.fullmatch(value)
        if match is None:
            continue
        result = match.groupdict()
        try:
            if format_name == "time-first-full":
                _parse_time_first(result, 4, timezone_hint)
            elif format_name == "time-first-short":
                _parse_time_first(result, 2, timezone_hint)
            elif format_name == "short-year":
                result["started"] = _parse_datetime(result["clock"], result["date"], result["year"])
            elif format_name == "raw-yearless":
                if year_hint is None:
                    return None
                zone = timezone_hint if timezone_hint and re.fullmatch(r"[+-]\d{4}", timezone_hint) else None
                result["started"] = _parse_datetime(result["clock"], result["date"], str(int(year_hint)), zone)
                result["year"] = str(int(year_hint))
                result["zone"] = zone or ""
            elif format_name == "full-year":
                result["started"] = _parse_datetime(result["clock"], result["date"], result["year"], result.get("zone"))
            else:
                result["started"] = datetime.strptime(
                    result["clock"] + "_" + result["date"], "%H%M%S_%d%m%y"
                ).astimezone()
                result["year"] = result["started"].strftime("%Y")
        except (ValueError, TypeError, OverflowError):
            return None
        result["legacy"] = format_name in ("intermediate", "legacy")
        result["format"] = format_name
        result["kind"] = result.get("kind") or "RAW"
        return result
    return None


def parse_path(path: str | Path) -> dict | None:
    """Parse a media path; use parent folders for older year/zone-less names."""
    item = Path(path)
    year_hint = None
    timezone_hint = None
    for parent in item.parents:
        name = parent.name
        if year_hint is None and re.fullmatch(r"\d{4}", name):
            year_hint = name
        if timezone_hint is None and re.fullmatch(r"[+-]\d{4}", name):
            timezone_hint = name
    return parse(item.stem, year_hint=year_hint, timezone_hint=timezone_hint)
