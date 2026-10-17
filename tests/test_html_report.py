"""Standalone HTML invariants: unchanged evidence, clocks, quality and isolation."""
import copy
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict_html.py"


@pytest.fixture
def html_report():
    spec = importlib.util.spec_from_file_location("_standalone_html_report_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reviewed_data(status="pass"):
    """A deliberately gapped future calendar and already-filtered valid paths."""
    days = ["2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11", "2024-10-14"]
    forecast = [
        {"date": day, "close": close, "close_p10": close - .8,
         "close_p90": close + .8, "volume": 1200000 + i * 10000}
        for i, (day, close) in enumerate(zip(days, [101, 102, 101.5, 104, 103]))
    ]
    samples = [
        [[row["close"] + offset - .1, row["close"] + offset + .3,
          row["close"] + offset - .3, row["close"] + offset, 1200000, 120000000]
         for row in forecast]
        for offset in (-1, 1)
    ]
    quality = {"prediction_usable": status == "pass", "requested_samples": 4,
               "valid_samples": 2, "invalid_samples": 2, "valid_fraction": .5,
               "valid_mask": [True, False, True, False],
               "minimum_valid_samples": 2, "minimum_valid_fraction": .5}
    prediction = {"status": status, "reason": "Reviewed software fixture",
                  "dates": days, "forecast": forecast, "samples": samples,
                  "valid_mask": quality["valid_mask"], "quality": quality,
                  "parameters": {"horizon": 5, "samples": 4},
                  "raw_samples_available": True, "warnings": []}
    if status in ("unavailable", "not_executed"):
        prediction.update(dates=[], forecast=[], samples=[], valid_mask=[], quality={},
                          raw_samples_available=False)
    bars = [
        {"time": "2024-09-27", "open": 99.5, "high": 100.25, "low": 99.125,
         "close": 99.875, "volume": 1100000, "is_closed": True,
         "volume_mean20_prior": 1050123.5},
        {"time": "2024-09-30", "open": 99.875, "high": 100.5, "low": 99.75,
         "close": 100.0, "volume": 1150000, "is_closed": True,
         "volume_mean20_prior": 1051534.25},
    ]
    return {
        "schema": "kcpredict_chart_data_v1",
        "meta": {"symbol": "600519.SH", "requested_end": "2024-09-30",
                 "as_of": "2024-09-30", "data_end": "2024-09-30", "months": 3,
                 "timezone": "Asia/Shanghai", "price_basis": "unadjusted",
                 "history_fingerprint": "synthetic-history-hash",
                 "data_version": "synthetic-html-validation-v1",
                 "data_provenance": {"provenance": "synthetic_test_fixture"}},
        "history": {"bars": bars}, "pens": [], "zones": [],
        "prediction": prediction,
        "quality": {"prediction_status": status,
                    "prediction_usable": quality["prediction_usable"] if status in ("pass", "fail") else None},
        "structure": {"status": "unavailable", "direction_state": None,
                      "current_state": {}, "support_facts": []},
        "price_volume": {"status": "unavailable", "direction_state": None},
        "ict": {"status": "completed", "final_status": "not_assessed", "warnings": []},
        "source_files": {"/private/fixture-inputs/research.json": "synthetic-file-hash"},
    }


class HtmlInventory(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.external_references = []
        self._current = None
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        for key in ("src", "href"):
            if attrs.get(key, "").startswith(("http:", "https:", "//")):
                self.external_references.append((tag, key, attrs[key]))
        if tag == "script":
            self._current = {"attrs": attrs, "text": ""}

    def handle_data(self, text):
        if self._current is not None:
            self._current["text"] += text

    def handle_endtag(self, tag):
        if tag == "script" and self._current is not None:
            self.scripts.append(self._current)
            self._current = None


def embedded_payload(path):
    inventory = HtmlInventory(path.read_text(encoding="utf-8"))
    data_scripts = [script for script in inventory.scripts
                    if script["attrs"].get("type") == "application/json"]
    assert len(data_scripts) == 1
    return json.loads(data_scripts[0]["text"]), inventory


@pytest.mark.parametrize("status", ["pass", "fail", "unavailable", "not_executed"])
def test_four_quality_states_remain_distinct_in_generated_html(html_report, tmp_path, status):
    data = reviewed_data(status)
    before = copy.deepcopy(data)
    target = tmp_path / f"{status}.html"
    artifact = html_report.render_html(data, target)
    embedded, inventory = embedded_payload(target)
    assert data == before
    assert artifact["prediction_status"] == embedded["prediction"]["status"] == status
    assert embedded["meta"]["symbol"] == "600519.SH"
    assert embedded["meta"]["data_end"] == "2024-09-30"
    assert embedded["bars"] == data["history"]["bars"]
    assert embedded["prediction"]["quality"] == data["prediction"]["quality"]
    assert embedded["prediction"]["forecast"] == data["prediction"]["forecast"]
    assert embedded["prediction"]["samples"] == data["prediction"]["samples"]
    assert not inventory.external_references
    assert all("src" not in script["attrs"] for script in inventory.scripts)
    assert len(inventory.scripts) == 2


def test_future_calendar_and_valid_paths_are_copied_without_padding_or_refiltering(html_report):
    data = reviewed_data()
    payload = html_report.build_payload(data)
    assert payload["prediction"]["dates"] == [
        "2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11", "2024-10-14"]
    assert len(payload["prediction"]["samples"]) == 2
    assert len(payload["prediction"]["valid_mask"]) == 4
    assert payload["bars"][-1]["close"] == 100.0
    assert payload["bars"][-1]["volume_mean20_prior"] == 1051534.25


def test_generated_html_embeds_shared_style_identity_and_css_without_changing_evidence(html_report, tmp_path):
    data = reviewed_data("fail")
    before = copy.deepcopy(data)
    target = tmp_path / "shared-style.html"
    artifact = html_report.render_html(data, target)
    embedded, inventory = embedded_payload(target)
    identity = html_report.theme_identity(html_report.load_theme())
    assert artifact["theme"] == embedded["theme_identity"] == identity
    assert embedded["theme"] == html_report.load_theme()
    assert embedded["prediction"] == html_report.build_payload(data)["prediction"]
    assert embedded["bars"] == data["history"]["bars"]
    assert data == before
    text = target.read_text(encoding="utf-8")
    assert "--paper:" + embedded["theme"]["palette"]["paper"] in text
    assert "--up:" + embedded["theme"]["palette"]["up"] in text
    assert "__THEME_CSS__" not in text and "__MOBILE_BREAKPOINT_PX__" not in text
    assert not inventory.external_references


def test_style_changes_update_shared_colors_and_identity_but_preserve_paths_and_quality(html_report, monkeypatch, tmp_path):
    data = reviewed_data("fail")
    changed = html_report.load_theme()
    original_identity = html_report.theme_identity(changed)
    changed["palette"].update(paper="#efefef", up="#c87a65", blue="#4066aa")
    changed["typography"]["body_px"] = 15
    monkeypatch.setattr(html_report, "load_theme", lambda: copy.deepcopy(changed))
    target = tmp_path / "new-theme.html"
    artifact = html_report.render_html(data, target)
    embedded, _ = embedded_payload(target)
    assert artifact["theme"] != original_identity
    assert embedded["theme"]["palette"] == changed["palette"]
    assert embedded["prediction"]["samples"] == data["prediction"]["samples"]
    assert embedded["prediction"]["forecast"] == data["prediction"]["forecast"]
    assert embedded["prediction"]["quality"] == data["prediction"]["quality"]
    assert embedded["prediction"]["status"] == "fail"
    assert "--body-px:15px" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["historical_forecast", "unordered_forecast", "different_calendar", "open_history"])
def test_future_dates_never_enter_closed_history(html_report, damage):
    data = reviewed_data()
    if damage == "historical_forecast":
        data["prediction"]["forecast"][0]["date"] = "2024-09-30"
        data["prediction"]["dates"][0] = "2024-09-30"
    elif damage == "unordered_forecast":
        data["prediction"]["forecast"][1]["date"] = "2024-10-07"
        data["prediction"]["dates"][1] = "2024-10-07"
    elif damage == "different_calendar":
        data["prediction"]["dates"][0] = "2024-10-01"
    else:
        data["history"]["bars"][-1]["is_closed"] = False
    with pytest.raises(ValueError):
        html_report.build_payload(data)


def test_unavailable_prediction_cannot_retain_an_unrendered_future_calendar(html_report):
    data = reviewed_data("unavailable")
    data["prediction"]["dates"] = ["2024-10-08"]
    with pytest.raises(ValueError):
        html_report.build_payload(data)


@pytest.mark.parametrize("status", ["fail", "unavailable", "not_executed"])
def test_quality_status_cannot_contradict_a_true_usability_flag(html_report, status):
    data = reviewed_data(status)
    data["prediction"]["quality"]["prediction_usable"] = True
    with pytest.raises(ValueError):
        html_report.build_payload(data)


def test_pass_requires_actual_future_rows_but_zero_valid_sample_failure_stays_failed(html_report):
    data = reviewed_data("pass")
    data["prediction"].update(forecast=[], dates=[], samples=[])
    with pytest.raises(ValueError):
        html_report.build_payload(data)
    data = reviewed_data("fail")
    data["prediction"].update(forecast=[], dates=[], samples=[], valid_mask=[False] * 4)
    data["prediction"]["quality"].update(valid_samples=0, invalid_samples=4,
                                            valid_fraction=0, valid_mask=[False] * 4)
    assert html_report.build_payload(data)["prediction"]["status"] == "fail"


def test_user_text_cannot_break_out_of_the_embedded_json_script(html_report, tmp_path):
    data = reviewed_data("unavailable")
    attack = '</script><script>globalThis.injected=true</script>&<img src="https://invalid.example/x">\u2028\u2029'
    data["prediction"]["reason"] = attack
    data["ict"]["warnings"] = [attack]
    data["structure"]["current_state"]["native_labels"] = [attack]
    target = tmp_path / "escaped.html"
    html_report.render_html(data, target)
    embedded, inventory = embedded_payload(target)
    assert embedded["prediction"]["reason"] == attack
    assert embedded["ict"]["warnings"] == [attack]
    assert len(inventory.scripts) == 2
    assert not inventory.external_references
    data_script = next(script["text"] for script in inventory.scripts
                       if script["attrs"].get("type") == "application/json")
    assert "<" not in data_script and ">" not in data_script


def test_existing_html_is_never_overwritten(html_report, tmp_path):
    target = tmp_path / "existing.html"
    target.write_bytes(b"Keep original artifact")
    with pytest.raises(FileExistsError):
        html_report.render_html(reviewed_data(), target)
    assert target.read_bytes() == b"Keep original artifact"


def test_portable_payload_excludes_private_source_and_execution_paths(html_report, tmp_path):
    data = reviewed_data("unavailable")
    secret_path = "/private/research-owner/data/source.csv"
    data["prediction"]["command"] = {"argv": ["python", secret_path], "stderr": f"Cannot open {secret_path}"}
    data["meta"]["data_provenance"]["source_files"] = [{"path": secret_path, "sha256": "csv-hash"}]
    data["ict"]["source"] = {"data_provenance": copy.deepcopy(data["meta"]["data_provenance"])}
    target = tmp_path / "portable.html"
    html_report.render_html(data, target)
    embedded, _ = embedded_payload(target)
    assert secret_path not in json.dumps(embedded)
    assert embedded["source_hashes"] == [{"name": "research.json", "sha256": "synthetic-file-hash"}]
    assert embedded["prediction"]["status"] == "unavailable"


def test_standalone_javascript_is_syntactically_valid_when_node_is_available(html_report, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available")
    target = tmp_path / "syntax.html"
    html_report.render_html(reviewed_data(), target)
    _, inventory = embedded_payload(target)
    source = next(script["text"] for script in inventory.scripts
                  if script["attrs"].get("type") != "application/json")
    javascript = tmp_path / "viewer.js"
    javascript.write_text(source, encoding="utf-8")
    result = subprocess.run([node, "--check", str(javascript)], check=False,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
