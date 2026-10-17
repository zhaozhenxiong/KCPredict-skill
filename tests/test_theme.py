"""One portable style source and a stable identity for every report renderer."""
import copy
import importlib.util
import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict_theme.py"
ASSET = ROOT / "skill" / "assets" / "report-theme.json"


@pytest.fixture
def theme_module():
    spec = importlib.util.spec_from_file_location("_report_theme_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_asset_keeps_the_established_html_palette(theme_module):
    theme = theme_module.load_theme()
    assert theme["schema"] == "kcpredict_report_theme_v1"
    assert theme["id"] == "kcpredict-paper-v1"
    expected = {"paper": "#f5f6f2", "card": "#ffffff", "ink": "#203c48",
                "up": "#c86c58", "down": "#3c8b80", "blue": "#4275cd",
                "gold": "#b28f43", "purple": "#8c77ac", "amber": "#a56327"}
    assert {key: theme["palette"][key] for key in expected} == expected
    assert theme_module.CORE_COLORS.issubset(theme["palette"])
    assert theme["layout"]["chart_height_px"] == 550
    assert theme["typography"]["body_px"] == 14


def test_theme_loads_are_independent_and_hash_ignores_json_formatting(theme_module, tmp_path):
    first = theme_module.load_theme()
    original = copy.deepcopy(first)
    first["palette"]["ink"] = "#000000"
    assert theme_module.load_theme() == original
    reordered = dict(reversed(list(original.items())))
    target = tmp_path / "theme.json"
    target.write_text(json.dumps(reordered, ensure_ascii=False, indent=7), encoding="utf-8-sig")
    assert theme_module.theme_identity(theme_module.load_theme(target)) == theme_module.theme_identity(original)
    assert re.fullmatch(r"[0-9a-f]{64}", theme_module.theme_sha256())
    assert theme_module.theme_sha256(first) != theme_module.theme_sha256(original)


def test_css_variables_share_the_only_up_down_color_source(theme_module):
    theme = theme_module.load_theme()
    theme["palette"]["up"] = "#123456"
    css = theme_module.theme_css_variables(theme)
    assert "--up:#123456" in css and "--red:#123456" in css
    assert "--teal:" + theme["palette"]["down"] in css
    assert "--body-px:14px" in css
    assert "--chart-height-px:550px" in css
    assert "--font-stack:" + theme["typography"]["font_stack"] in css


@pytest.mark.parametrize("damage", ["schema", "missing_color", "color_expression", "color_escape", "font_escape", "negative_layout", "nonfinite_size", "layout_name"])
def test_invalid_or_injectable_theme_never_silently_falls_back(theme_module, tmp_path, damage):
    theme = theme_module.load_theme()
    if damage == "schema":
        theme["schema"] = "unknown"
    elif damage == "missing_color":
        theme["palette"].pop("blue")
    elif damage == "color_expression":
        theme["palette"]["blue"] = "url(https://invalid.example/color)"
    elif damage == "color_escape":
        theme["palette"]["blue"] = "#123456;</style><script>bad()</script>"
    elif damage == "font_escape":
        theme["typography"]["font_stack"] = "serif; background:red"
    elif damage == "negative_layout":
        theme["layout"]["chart_height_px"] = -1
    elif damage == "nonfinite_size":
        theme["typography"]["body_px"] = float("nan")
    else:
        theme["layout"]["x;color"] = 10
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(theme), encoding="utf-8")
    with pytest.raises(ValueError):
        theme_module.load_theme(target)


def test_template_has_no_separate_core_color_constants(theme_module):
    template = (ASSET.parent / "composite.html").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", template)
    assert template.count("__THEME_CSS__") == 1
    assert "palette=theme.palette" in template
    assert "up:palette.up" in template and "down:palette.down" in template
    assert "palette.zone_fill" in template and "palette.band" in template


def test_missing_theme_asset_is_an_explicit_error(theme_module, tmp_path):
    with pytest.raises(FileNotFoundError):
        theme_module.load_theme(tmp_path / "missing.json")
