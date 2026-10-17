#!/usr/bin/env python3
"""Load the shared report style without touching research or forecast evidence.

The JSON asset is the single source of report colors. HTML and static renderers
consume the same normalized document and canonical SHA256 identity. A missing
or malformed theme fails explicitly instead of silently choosing other colors.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


THEME_PATH = Path(__file__).resolve().parents[1] / "assets" / "report-theme.json"
SCHEMA = "kcpredict_report_theme_v1"
CORE_COLORS = {
    "paper", "card", "ink", "muted", "line", "up", "down", "blue", "gold", "purple", "amber",
    "band", "band_edge", "sample", "forecast_top", "forecast_bottom", "zone_fill", "zone_fill_muted",
    "zone_edge", "fvg", "cutoff", "close_reference", "volume_future", "volume_grid", "grid", "crosshair",
}
FONT_FIELDS = {"font_stack", "mono_font_stack"}
TYPE_FIELDS = {"body_px", "chart_px", "title_px", "quote_px", "code_px", "body_line_height"}
LAYOUT_FIELDS = {"max_width_px", "chart_height_px", "mobile_chart_height_px", "mobile_breakpoint_px"}


def _validate(theme: Any) -> dict:
    if not isinstance(theme, dict) or theme.get("schema") != SCHEMA:
        raise ValueError(f"Report theme must use {SCHEMA}")
    if not isinstance(theme.get("id"), str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", theme["id"]) is None:
        raise ValueError("Report theme id must be a nonempty lowercase identifier")
    palette, typography, layout = (theme.get(key) for key in ("palette", "typography", "layout"))
    if not isinstance(palette, dict) or not CORE_COLORS.issubset(palette):
        raise ValueError("Report theme palette is missing required semantic colors")
    for key, value in palette.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError("Palette names must be lowercase semantic identifiers")
        if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value) is None:
            raise ValueError(f"Theme color {key} must be a six- or eight-digit hex value")
    if not isinstance(typography, dict) or not (FONT_FIELDS | TYPE_FIELDS).issubset(typography):
        raise ValueError("Report theme typography is incomplete")
    for field in FONT_FIELDS:
        value = typography[field]
        if not isinstance(value, str) or not value.strip() or re.search(r"[<>{};\\\r\n]", value):
            raise ValueError(f"Theme {field} must be a CSS font-family list without declarations")
    if not isinstance(layout, dict) or not LAYOUT_FIELDS.issubset(layout):
        raise ValueError("Report theme layout is incomplete")
    if any(not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None for key in layout):
        raise ValueError("Layout names must be lowercase semantic identifiers")
    for section, fields in ((typography, TYPE_FIELDS), (layout, set(layout))):
        for field in fields:
            value = section[field]
            try:
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"Theme {field} must be finite and positive")
    # Extra metadata is allowed for audit/versioning, but the whole document
    # must remain strict JSON so every renderer hashes the same content.
    json.dumps(theme, ensure_ascii=False, allow_nan=False)
    return copy.deepcopy(theme)


def load_theme(path: Path | str | None = None) -> dict:
    """Return an independently owned, validated theme from the portable asset."""
    location = THEME_PATH if path is None else Path(path)
    return _validate(json.loads(location.read_text(encoding="utf-8-sig")))


def theme_sha256(theme: dict | None = None) -> str:
    """Canonical document identity, independent of JSON whitespace/key order."""
    value = load_theme() if theme is None else _validate(theme)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def theme_identity(theme: dict | None = None) -> dict:
    value = load_theme() if theme is None else _validate(theme)
    return {"schema": value["schema"], "id": value["id"], "sha256": theme_sha256(value)}


def theme_css_variables(theme: dict | None = None) -> str:
    """Inline CSS variables; color/font values have passed a narrow validation."""
    value = load_theme() if theme is None else _validate(theme)
    variables = {key.replace("_", "-"): color for key, color in value["palette"].items()}
    # The original HTML names remain aliases of the sole up/down color source.
    variables.update(red=value["palette"]["up"], teal=value["palette"]["down"])
    for key in sorted(FONT_FIELDS | TYPE_FIELDS):
        item = value["typography"][key]
        variables[key.replace("_", "-")] = str(item) + ("px" if key.endswith("_px") else "")
    for key, item in value["layout"].items():
        variables[key.replace("_", "-")] = str(item) + ("px" if key.endswith("_px") else "")
    return ":root{" + ";".join(f"--{key}:{item}" for key, item in variables.items()) + "}"
