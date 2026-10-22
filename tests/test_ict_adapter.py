"""Closed-history and provenance boundaries for the standalone ICT bridge."""
import copy
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict_ict.py"


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("_kcpredict_ict_bridge", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def structure(count=16):
    start = date(2026, 1, 1)
    bars = [{"time": (start + timedelta(days=index)).isoformat(), "open": 10 + index * .1,
             "high": 11 + index * .1, "low": 9 + index * .1, "close": 10.2 + index * .1,
             "volume": 1000, "is_closed": True} for index in range(count)]
    return {"schema": "kcpredict_structure_v1", "symbol": "000001.SZ", "as_of": bars[-1]["time"] if bars else "2026-01-16",
            "data_end": bars[-1]["time"] if bars else "2026-01-16", "price_basis": "unadjusted",
            "frequencies": {name: {"bars": copy.deepcopy(bars)} for name in ("日线", "周线", "月线")}}


@pytest.mark.parametrize("frequency", ["周线", "月线"])
def test_forming_higher_periods_and_nonboolean_closure_are_excluded(adapter, frequency):
    payload = structure()
    bars = payload["frequencies"][frequency]["bars"]
    bars[-1]["is_closed"] = False
    bars[-2]["is_closed"] = 1
    del bars[-3]["is_closed"]
    result = adapter.adapt(payload, input_sha256="abc", frequency=frequency)
    assert result["selection"]["eligible_closed_bars"] == 13
    assert result["selection"]["excluded_bars"] == 3
    assert result["selection"]["exclusion_reasons"]["not_closed"] == 3
    assert result["scan"]["meta"]["candle_count"] == 13


def test_as_of_and_bar_observation_clocks_both_exclude_future(adapter):
    payload = structure()
    payload["as_of"] = "2026-01-14T14:59:59+08:00"
    bars = payload["frequencies"]["日线"]["bars"]
    bars[10]["observed_at"] = "2026-01-15T15:00:00+08:00"
    result = adapter.adapt(payload, input_sha256="abc")
    assert result["selection"]["eligible_closed_bars"] == 12
    assert result["selection"]["excluded_bars"] == 4
    assert result["selection"]["exclusion_reasons"]["bar_after_as_of"] == 3
    assert result["selection"]["exclusion_reasons"]["observed_after_as_of"] == 1
    assert result["scan"]["meta"]["last_candle"] == "2026-01-13T15:00:00+08:00"


def test_outer_and_structure_data_end_cannot_be_extended_by_as_of(adapter):
    payload = structure()
    payload["data_end"] = "2026-01-14"
    research = {"schema": "kcpredict_research_v1", "symbol": "000001.SZ", "data_end": "2026-01-13",
                "requested_end": "2026-01-20", "analysis": payload}
    payload["frequencies"]["日线"]["bars"][10]["observed_at"] = "2026-01-14T15:00:00+08:00"
    result = adapter.adapt(research, input_sha256="abc")
    assert result["selection"]["eligible_closed_bars"] == 12
    assert result["selection"]["excluded_bars"] == 4
    assert result["source"]["data_end"] == "2026-01-13"
    assert result["source"]["analysis_data_end"] == "2026-01-14"


def test_forecast_is_never_read_or_used_to_reach_minimum_history(adapter):
    class ForbiddenForecast:
        def __deepcopy__(self, memo):
            raise AssertionError("prediction must never be inspected")
        def __iter__(self):
            raise AssertionError("prediction must never be inspected")
    payload = {"schema": "kcpredict_research_v1", "symbol": "000001.SZ", "data_end": "2026-01-09",
               "analysis": structure(9), "prediction": ForbiddenForecast()}
    result = adapter.adapt(payload, input_sha256="abc")
    assert result["status"] == "unavailable"
    assert result["reason_codes"] == ["insufficient_closed_history"]
    assert result["scan"] is None


def test_upstream_unavailable_qualification_is_retained_without_large_history(adapter):
    payload = structure()
    payload["source_evidence"] = {"status": "partial", "semantics": {"scope": "research_reference"},
                                  "events": [{"a_large_history": True}]}
    payload["price_volume"] = {"input_eligible": False, "reason_codes": ["native_verified_calendar_unavailable"],
                               "rows": [{"a_large_history": True}]}
    result = adapter.adapt(payload, input_sha256="abc")
    assert result["source"]["source_evidence"] == {"status": "partial", "semantics": {"scope": "research_reference"}}
    assert result["source"]["price_volume"] == {
        "input_eligible": False, "reason_codes": ["native_verified_calendar_unavailable"]}
    assert result["qualification"]["formal_qualified"] is False
    assert result["final_status"] == "not_assessed"


def test_input_dates_are_exchange_closes_and_output_never_uses_ny_sessions(adapter, monkeypatch):
    seen = {}
    def scan(candles, meta, **parameters):
        seen.update(candles=candles, meta=meta, parameters=parameters)
        candidate = {"mechanical_status": "confirmed", "final_status_requires_manual_context": True,
                     "sweep": {"time": "2026-01-05T07:00:00Z", "ny_time": "2026-01-05T02:00:00-05:00",
                               "session": "london_focus"}}
        return {"meta": {"timezone_for_sessions": "America/New_York"},
                "latest_candidate": candidate, "candidates": [candidate]}
    monkeypatch.setattr(adapter.ict_scan, "scan", scan)
    result = adapter.adapt(structure(), input_sha256="abc", direction="short", swing_width=3, minimum_rr=3)
    assert seen["candles"][0].timestamp.isoformat() == "2026-01-01T07:00:00+00:00"
    assert seen["candles"][0].is_closed is True
    assert result["selection"]["first_bar"] == "2026-01-01T15:00:00+08:00"
    assert seen["parameters"] == {"direction": "short", "swing_width": 3, "minimum_rr": 3}
    assert result["scan"]["meta"]["timezone_for_sessions"] == "Asia/Shanghai"
    candidate = result["scan"]["latest_candidate"]
    assert candidate["mechanical_status"] == "confirmed"
    assert candidate["sweep"]["session"] == "not_applicable_daily_or_higher"
    assert candidate["sweep"]["exchange_time"] == "2026-01-05T15:00:00+08:00"
    assert "ny_time" not in candidate["sweep"]
    assert result["final_status"] == "not_assessed"
    assert result["final_status_requires_manual_context"] is True
    assert set(result["qualification"]["unverified_context"].values()) == {"unverified"}


def test_zero_closed_bars_is_auditable_unavailable(adapter):
    payload = structure()
    for row in payload["frequencies"]["日线"]["bars"]:
        row["is_closed"] = False
    result = adapter.adapt(payload, input_sha256="abc")
    assert result["status"] == "unavailable"
    assert result["selection"]["excluded_bars"] == 16
    assert result["selection"]["eligible_closed_bars"] == 0
    assert result["selection"]["first_bar"] is None


def run_cli(input_path, *arguments, cwd):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    if os.name == "nt":
        import tzdata
        # -S hides site-packages; supply IANA data like a Unix system TZPATH.
        environment["PYTHONTZPATH"] = str((Path(tzdata.__file__).parent / "zoneinfo").resolve())
    return subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--input", str(input_path), *arguments],
                          cwd=cwd, env=environment, capture_output=True, text=True)


def test_cli_runs_from_unrelated_directory_without_package_and_retains_provenance(tmp_path):
    payload = {"schema": "kcpredict_research_v1", "symbol": "000001.SZ", "requested_end": "2026-01-16",
               "data_end": "2026-01-16", "data_provenance": {"source": "user_asserted"},
               "calendar_identity": {"verified": True}, "analysis": structure(),
               "prediction": {"forecast": [{"date": "2099-01-01", "close": 999999}]}}
    source = tmp_path / "research with spaces.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = run_cli(source, "--frequency", "周线", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["schema"] == "kcpredict_ict_v1"
    assert actual["source"]["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert actual["source"]["calendar_identity"] == {"verified": True}
    assert actual["source"]["data_provenance"] == {"source": "user_asserted"}
    assert actual["scan"]["meta"]["last_candle"] == "2026-01-16T15:00:00+08:00"
    assert "2099-01-01" not in result.stdout


def test_cli_unavailable_returns_json_without_traceback_and_writes_new_output(tmp_path):
    source = tmp_path / "short.json"
    source.write_text(json.dumps(structure(9)), encoding="utf-8")
    destination = tmp_path / "result.json"
    result = run_cli(source, "--output", str(destination), cwd=tmp_path)
    assert result.returncode == 2, result.stderr
    assert json.loads(destination.read_text())["status"] == "unavailable"
    assert "Traceback" not in result.stderr
    repeated = run_cli(source, "--output", str(destination), cwd=tmp_path)
    assert repeated.returncode == 1
    assert json.loads(repeated.stderr)["error"] == "FileExistsError"
    assert json.loads(destination.read_text())["status"] == "unavailable"


def test_cli_rejects_non_kcpredict_input_without_creating_output(tmp_path):
    source = tmp_path / "generic.json"
    source.write_text(json.dumps({"candles": []}), encoding="utf-8")
    destination = tmp_path / "never-created.json"
    result = run_cli(source, "--output", str(destination), cwd=tmp_path)
    assert result.returncode == 1
    assert "input schema" in json.loads(result.stderr)["message"]
    assert not destination.exists()


@pytest.mark.parametrize("change", ["nonfinite", "duplicate", "invalid_geometry"])
def test_invalid_closed_history_fails_closed_without_silent_repair(adapter, change):
    payload = structure()
    bars = payload["frequencies"]["日线"]["bars"]
    if change == "nonfinite":
        bars[3]["close"] = float("nan")
    elif change == "duplicate":
        bars[3]["time"] = bars[2]["time"]
    else:
        bars[3]["low"] = 100
    with pytest.raises(ValueError):
        adapter.adapt(payload, input_sha256="abc")
