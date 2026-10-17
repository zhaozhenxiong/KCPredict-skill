"""Renderer contracts, source compatibility and confirmation-time boundaries."""
import copy
from datetime import date, timedelta
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict_chart_data.py"


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("_kcpredict_chart_data_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def structure(*, count=152, end=date(2024, 5, 31), symbol="600000.SH"):
    start = end - timedelta(days=count - 1)
    bars = [{"time": (start + timedelta(days=index)).isoformat(), "open": 10 + index * .01,
             "high": 11 + index * .01, "low": 9 + index * .01, "close": 10.2 + index * .01,
             "volume": 100 + index, "is_closed": True} for index in range(count)]
    return {"schema": "kcpredict_structure_v1", "symbol": symbol, "as_of": end.isoformat(),
            "data_end": end.isoformat(), "data_version": "fixture-version-v1", "price_basis": "unadjusted",
            "frequencies": {"日线": {"bars": bars, "pens": [], "zones": []}}}


def forecast(payload, *, count=8, valid=8, usable=True):
    last = payload["frequencies"]["日线"]["bars"][-1]
    base = last["close"]
    days = [(date.fromisoformat(last["time"]) + timedelta(days=i + 1)).isoformat() for i in range(2)]
    raw = []
    for path in range(count):
        rows = []
        for index in range(2):
            close = base + 1 + index * .2 + path * .01
            rows.append([close - .1, close + .5, close - .5, close, 200, 2000])
        if path >= valid:
            rows[0][1] = rows[0][2] - 1  # Entire path fails OHLC geometry.
        raw.append(rows)
    result = []
    if valid:
        for index, day in enumerate(days):
            close = base + 1 + index * .2 + (valid - 1) * .005
            result.append({"date": day, "open": close - .1, "high": close + .5, "low": close - .5,
                           "close": close, "volume": 200, "amount": 2000,
                           "close_p10": base + 1 + index * .2 + (valid - 1) * .001,
                           "close_p90": base + 1 + index * .2 + (valid - 1) * .009,
                           "close_mean": close})
    return {"schema": "kronos_forecast_v1", "status": "completed", "symbol": payload["symbol"],
            "as_of": last["time"], "input": {"symbol": payload["symbol"], "as_of": last["time"],
                "history_end": last["time"], "last_close": last["close"], "horizon": 2, "price_basis": "unadjusted"},
            "identity": {"future_dates": days, "parameters": {"samples": count, "horizon": 2, "seed": 42}},
            "quality": {"requested_samples": count, "valid_samples": valid, "invalid_samples": count - valid,
                "valid_fraction": valid / count, "valid_mask": [i < valid for i in range(count)],
                "minimum_valid_samples": 8, "minimum_valid_fraction": .8, "prediction_usable": usable},
            "forecast": result, "samples": raw, "summary": None}


def notes_for_sweep(payload):
    """A known old low, an opposing confirmed high, then a valid SSL rejection."""
    bars = payload["frequencies"]["日线"]["bars"]
    bars[-8].update(open=10, high=11, low=8, close=10)
    bars[-6].update(open=10, high=15, low=9, close=10)
    bars[-3].update(open=9, high=11, low=7.5, close=10)
    return {"schema": "kcpredict_chart_notes_v1", "symbol": payload["symbol"], "as_of": payload["as_of"],
            "items": [{"type": "ssl_candidate", "date": bars[-3]["time"], "reference_date": bars[-8]["time"],
                       "reference_price": 8, "extreme": 7.5, "close": 10,
                       "mss_reference_date": bars[-6]["time"], "mss_reference_price": 15,
                       "label": "An interpretation supplied by a human"}]}


def test_calendar_month_window_and_twenty_prior_bars(helper):
    result = helper.prepare_chart_data(structure(), months=3)
    assert result["history"]["requested_start"] == "2024-02-29"  # Leap-day clamp, not 90 days.
    assert result["history"]["date_start"] == "2024-02-29"
    assert result["history"]["date_end"] == "2024-05-31"
    assert result["history"]["warmup_bars"] == 20
    first = result["history"]["bars"][0]
    original = result["full_history"]
    index = next(i for i, row in enumerate(original) if row["time"] == first["time"])
    expected = sum(row["volume"] for row in original[index - 20:index]) / 20
    assert first["relative_volume20"] == pytest.approx(first["volume"] / expected)
    assert result["prediction"]["status"] == "not_executed"
    assert result["structure"]["status"] == "unavailable"


def test_missing_activity_and_evidence_are_not_filled_with_zero(helper):
    payload = structure()
    for row in payload["frequencies"]["日线"]["bars"]:
        row.pop("volume")
    result = helper.prepare_chart_data(payload)
    assert "volume" not in result["history"]["bars"][-1]
    assert result["history"]["bars"][-1]["relative_volume20"] is None
    assert result["price_volume"]["direction_state"] is None
    assert result["price_volume"]["w32_latest"] is None


def test_cutoffs_observation_clocks_and_closure_filter_history(helper):
    payload = structure(count=40)
    bars = payload["frequencies"]["日线"]["bars"]
    payload["as_of"] = bars[-2]["time"] + "T14:59:00+08:00"
    bars[-4]["is_closed"] = False
    bars[-5]["is_closed"] = 1
    bars[-6]["observed_at"] = bars[-1]["time"] + "T15:00:00+08:00"
    result = helper.prepare_chart_data(payload)
    actual = [row["time"] for row in result["full_history"]]
    assert actual[-1] == bars[-3]["time"]
    assert bars[-4]["time"] not in actual
    assert bars[-5]["time"] not in actual
    assert bars[-6]["time"] not in actual


def test_retrospective_zone_has_distinct_anchor_and_confirmation(helper):
    payload = structure()
    payload["frequencies"]["日线"]["zones"] = [
        {"start_time": "2024-03-01", "end_time": "2024-05-20", "low": 9.5, "high": 11,
         "confirmed_at": "2024-05-31T15:00:00+08:00", "is_confirmed": True},
        {"start_time": "2024-04-01", "end_time": "2024-05-25", "low": 9.8, "high": 11.5,
         "confirmed_at": "2024-06-01T15:00:00+08:00", "is_confirmed": True}]
    zones = helper.prepare_chart_data(payload)["zones"]
    assert zones[0]["start_time"] == "2024-03-01"
    assert zones[0]["display_confirmed"] is True
    assert zones[0]["is_retroactive"] is True
    assert zones[0]["confirmation_time"] == "2024-05-31T15:00:00+08:00"
    assert zones[1]["display_confirmed"] is False
    assert zones[1]["unavailable_as_of"] is True
    assert "未确认" in zones[1]["label"]


def test_future_observation_cannot_publish_a_forming_pen(helper):
    payload = structure()
    payload["frequencies"]["日线"]["pens"] = [
        {"start_time": "2024-04-01", "end_time": "2024-05-30", "start_price": 11, "end_price": 10,
         "is_confirmed": False, "observed_at": "2024-06-01T15:00:00+08:00"}]
    assert helper.prepare_chart_data(payload)["pens"] == []


def test_source_buy_sell_snapshots_keep_qualification_and_old_w32_separate(helper):
    payload = structure()
    snapshot = {"date": "2024-05-30", "available_at": "2024-05-30T15:00:00+08:00",
                "structure": {"date": "2024-05-30", "available_at": "2024-05-30T15:00:00+08:00",
                    "input_eligible": True, "direction_state": "BUY", "current_state": {"support_floor": 9.5},
                    "support_facts": [{"date": "2024-06-01", "available_at": "2024-06-01T15:00:00+08:00"}]},
                "wyckoff": {"date": "2024-05-30", "available_at": "2024-05-30T15:00:00+08:00",
                    "input_eligible": True, "direction_state": "SELL", "current_state": {"buy_score": .01, "sell_score": .33}}}
    future = copy.deepcopy(snapshot)
    future.update(date="2024-06-01", available_at="2024-06-01T15:00:00+08:00")
    payload["source_evidence"] = {"status": "partial", "evaluations": [snapshot], "latest": future}
    payload["price_volume"] = {"latest": {"date": "2024-05-31", "wyckoff_available_at": "2024-05-31T15:00:00+08:00",
        "wyckoff_state": "demand_retest_confirmed", "wyckoff_event": "none", "w_event_age": 76,
        "w_demand_score": 0, "w_supply_score": 0}}
    result = helper.prepare_chart_data(payload)
    assert result["structure"]["direction_state"] == "BUY"
    assert result["structure"]["status"] == "partial"
    assert result["structure"]["support_facts"] == []
    assert result["price_volume"]["direction_state"] == "SELL"
    assert result["price_volume"]["legacy_state_is_fresh"] is False
    assert result["price_volume"]["warnings"]


def test_arbitrary_symbol_forecast_pass_and_valid_paths(helper):
    payload = structure(symbol="000333.SZ")
    result = helper.prepare_chart_data(payload, forecast(payload))
    assert result["meta"]["symbol"] == "000333.SZ"
    assert result["prediction"]["status"] == "pass"
    assert len(result["prediction"]["samples"]) == 8
    assert result["prediction"]["validation"]["forecast_recomputed_and_compared"] is True


def test_failed_quality_stays_failed_and_invalid_paths_are_not_repaired(helper):
    payload = structure()
    prediction = forecast(payload, count=10, valid=7, usable=False)
    before = copy.deepcopy(prediction)
    result = helper.prepare_chart_data(payload, prediction)
    assert result["prediction"]["status"] == "fail"
    assert result["quality"]["prediction_usable"] is False
    assert len(result["prediction"]["samples"]) == 7
    assert result["prediction"]["quality"]["invalid_samples"] == 3
    assert prediction == before


def test_research_prediction_and_absent_raw_samples_are_supported(helper):
    payload = structure()
    prediction = forecast(payload)
    prediction.pop("samples")
    research = {"schema": "kcpredict_research_v1", "symbol": payload["symbol"], "data_end": payload["data_end"],
                "analysis": payload, "prediction": prediction}
    result = helper.prepare_chart_data(research)
    assert result["prediction"]["status"] == "pass"
    assert result["prediction"]["samples"] == []
    assert result["prediction"]["raw_samples_available"] is False
    assert any("absent" in warning for warning in result["prediction"]["warnings"])


@pytest.mark.parametrize("status", ["unavailable", "not_executed", "not_requested"])
def test_prediction_unavailable_contract_remains_explicit(helper, status):
    payload = structure()
    unavailable = {"schema": "kcpredict_prediction_unavailable_v1", "status": status, "symbol": payload["symbol"],
                   "as_of": payload["as_of"], "data_end": payload["data_end"], "reason": "user scope or command failure",
                   "command": {"exit_code": 1}, "parameters": {"samples": 32}}
    result = helper.prepare_chart_data(payload, unavailable)["prediction"]
    assert result["status"] == ("not_executed" if status == "not_requested" else status)
    assert result["forecast"] == []
    assert result["command"]["exit_code"] == 1


@pytest.mark.parametrize("field", ["symbol", "as_of", "last_close", "price_basis"])
def test_forecast_mismatch_cannot_be_drawn(helper, field):
    payload = structure()
    prediction = forecast(payload)
    if field == "symbol":
        prediction["symbol"] = "999999.SH"
    elif field == "as_of":
        prediction["as_of"] = "2024-05-30"
    elif field == "last_close":
        prediction["input"][field] += 1
    else:
        prediction["input"][field] = "adjusted"
    with pytest.raises(ValueError, match="disagrees"):
        helper.prepare_chart_data(payload, prediction)


@pytest.mark.parametrize("damage", ["shape", "mask", "quantile", "date", "quality_claim", "nonfinite"])
def test_corrupted_forecast_evidence_is_rejected_instead_of_repaired(helper, damage):
    payload = structure()
    prediction = forecast(payload)
    if damage == "shape":
        prediction["samples"][0][0].pop()
    elif damage == "mask":
        prediction["quality"]["valid_mask"][0] = False
    elif damage == "quantile":
        prediction["forecast"][0]["close_p90"] += .5
    elif damage == "date":
        prediction["forecast"][0]["date"] = payload["as_of"]
    elif damage == "quality_claim":
        prediction = forecast(payload, count=10, valid=7, usable=True)
    else:
        prediction["forecast"][0]["close"] = float("nan")
    with pytest.raises(ValueError):
        helper.prepare_chart_data(payload, prediction)


def test_forecast_parameters_and_summary_cannot_disagree_with_raw_paths(helper):
    payload = structure()
    prediction = forecast(payload)
    prediction["identity"]["parameters"]["samples"] = 32
    with pytest.raises(ValueError, match="parameters.samples"):
        helper.prepare_chart_data(payload, prediction)
    prediction = forecast(payload)
    prediction["summary"] = {"terminal_return_median": 100}
    with pytest.raises(ValueError, match="summary"):
        helper.prepare_chart_data(payload, prediction)
    prediction["summary"]["terminal_return_median"] = None
    with pytest.raises(ValueError, match="summary"):
        helper.prepare_chart_data(payload, prediction)


def test_forecast_lookback_span_must_match_real_history(helper):
    payload = structure()
    prediction = forecast(payload)
    prediction["input"].update(lookback=20, rows=20, history_start="2020-01-01")
    with pytest.raises(ValueError, match="history_start"):
        helper.prepare_chart_data(payload, prediction)


def test_rejected_nonfinite_raw_path_is_not_mixed_into_history_or_ict(helper):
    payload = structure()
    prediction = forecast(payload, count=10, valid=7, usable=False)
    prediction["samples"][-1][0][0] = None
    research = {"schema": "kcpredict_research_v1", "symbol": payload["symbol"], "data_end": payload["data_end"],
                "analysis": payload, "prediction": prediction}
    result = helper.prepare_chart_data(research)
    assert result["prediction"]["status"] == "fail"
    assert len(result["full_history"]) == len(payload["frequencies"]["日线"]["bars"])
    baseline = helper.prepare_chart_data(payload)
    assert result["ict"]["latest_candidate"] == baseline["ict"]["latest_candidate"]
    assert result["meta"]["history_fingerprint"] == baseline["meta"]["history_fingerprint"]
    assert result["ict"]["validation"]["source"] == "local_closed_history_adapter"


def test_zero_valid_sample_result_can_be_displayed_as_failed_without_paths(helper):
    payload = structure()
    result = helper.prepare_chart_data(payload, forecast(payload, count=8, valid=0, usable=False))
    assert result["prediction"]["status"] == "fail"
    assert result["prediction"]["forecast"] == []
    assert result["prediction"]["samples"] == []


def test_ict_is_derived_from_full_closed_history_and_provided_identity_is_checked(helper):
    payload = structure()
    local = helper.bridge.adapt(payload, input_sha256="original-file-byte-hash")
    result = helper.prepare_chart_data(payload, ict_payload=local)
    assert result["ict"]["validation"]["source"] == "supplied_payload_verified_against_closed_history"
    for key, value in (("symbol", "999999.SH"), ("as_of", "2024-05-30"), ("data_version", "wrong-version")):
        bad = copy.deepcopy(local)
        bad["source"][key] = value
        with pytest.raises(ValueError, match="ICT source"):
            helper.prepare_chart_data(payload, ict_payload=bad)
    bad = copy.deepcopy(local)
    bad["scan"]["counts"]["strict_fvgs"] += 1
    with pytest.raises(ValueError, match="detections disagree"):
        helper.prepare_chart_data(payload, ict_payload=bad)


def test_ict_custom_adapter_parameters_are_compatible(helper):
    payload = structure()
    local = helper.bridge.adapt(payload, input_sha256="bytes", direction="long", swing_width=1, minimum_rr=3)
    assert helper.prepare_chart_data(payload, ict_payload=local)["ict"]["status"] == "completed"


def test_unverifiable_ict_fingerprint_and_other_frequency_are_rejected(helper):
    payload = structure()
    payload.pop("data_version")
    local = helper.bridge.adapt(payload, input_sha256="unknown-file-bytes")
    with pytest.raises(ValueError, match="comparable source data fingerprint"):
        helper.prepare_chart_data(payload, ict_payload=local)
    local["frequency"] = "周线"
    with pytest.raises(ValueError, match="daily"):
        helper.prepare_chart_data(payload, ict_payload=local)


def test_latest_fvg_names_all_three_closed_bars_and_third_bar_confirmation(helper):
    payload = structure()
    bars = payload["frequencies"]["日线"]["bars"]
    bars[-3].update(open=10, high=10.5, low=9.7, close=10.2)
    bars[-2].update(open=10.3, high=13, low=10.2, close=12.7)
    bars[-1].update(open=12.7, high=13.2, low=12.4, close=13)
    gap = helper.prepare_chart_data(payload)["ict"]["latest_fvg"]
    assert gap["direction"] == "bullish"
    assert gap["zone"] == [10.5, 12.4]
    assert gap["candle_dates"] == [row["time"] for row in bars[-3:]]
    assert gap["confirmed_time"] == bars[-1]["time"] + "T15:00:00+08:00"
    assert gap["lifecycle"]["status"] == "active"
    assert gap["lifecycle"]["is_active"] is True
    assert gap["lifecycle"]["observed_bars_after_confirmation"] == 0
    assert gap["lifecycle"]["touch_date"] is None


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_fvg_lifecycle_retains_touch_fill_and_close_through_dates(helper, direction):
    payload = structure()
    bars = payload["frequencies"]["日线"]["bars"]
    bars[-6].update(open=10, high=10.5, low=9.7, close=10.2)
    bars[-5].update(open=10.3, high=13, low=10.2, close=12.7)
    bars[-4].update(open=12.7, high=13.2, low=12.4, close=13)
    bars[-3].update(open=13, high=13.5, low=11, close=12.5)  # partial touch
    bars[-2].update(open=12.5, high=13, low=10.4, close=11)  # full fill, close holds
    bars[-1].update(open=11, high=11.5, low=9.5, close=10)  # close through
    if direction == "bearish":
        for row in bars:
            old = {field: row[field] for field in ("open", "high", "low", "close")}
            row.update(open=30 - old["open"], high=30 - old["low"], low=30 - old["high"], close=30 - old["close"])
    result = helper.prepare_chart_data(payload)
    gap = next(gap for gap in result["ict"]["recent_fvgs"] if gap["candle_dates"][-1] == bars[-4]["time"])
    life = gap["lifecycle"]
    assert life["status"] == "closed_through"
    assert life["is_active"] is False
    assert life["touch_date"] == bars[-3]["time"]
    assert life["full_fill_date"] == bars[-2]["time"]
    assert life["close_through_date"] == bars[-1]["time"]


def test_fvg_gap_close_through_does_not_invent_a_traded_fill(helper):
    bars = [{"time": "2024-01-01", "timestamp": "2024-01-01T15:00:00+08:00", "high": 13, "low": 12, "close": 12.5},
            {"time": "2024-01-02", "timestamp": "2024-01-02T15:00:00+08:00", "high": 9, "low": 8, "close": 8.5}]
    life = helper._fvg_lifecycle({"zone": [10, 11], "direction": "bullish", "end_index": 0}, bars)
    assert life["status"] == "closed_through"
    assert life["touch_date"] is None
    assert life["full_fill_date"] is None


def test_close_through_ends_fvg_even_if_price_later_revisits(helper):
    bars = [{"time": "2024-01-01", "timestamp": "2024-01-01T15:00:00+08:00", "high": 13, "low": 12, "close": 12.5},
            {"time": "2024-01-02", "timestamp": "2024-01-02T15:00:00+08:00", "high": 9, "low": 8, "close": 8.5},
            {"time": "2024-01-03", "timestamp": "2024-01-03T15:00:00+08:00", "high": 12, "low": 9, "close": 11.5}]
    life = helper._fvg_lifecycle({"zone": [10, 11], "direction": "bullish", "end_index": 0}, bars)
    assert life["status"] == "closed_through"
    assert life["touch_date"] is None
    assert life["close_through_date"] == "2024-01-02"


def test_unavailable_source_direction_is_audit_context_not_qualified_buy_sell(helper):
    payload = structure()
    day = payload["as_of"]
    payload["source_evidence"] = {"status": "unavailable", "latest": {
        "date": day, "available_at": day + "T15:00:00+08:00",
        "structure": {"date": day, "available_at": day + "T15:00:00+08:00", "input_eligible": True,
                      "direction_state": "BUY", "current_state": {}}}}
    layer = helper.prepare_chart_data(payload)["structure"]
    assert layer["status"] == "unavailable"
    assert layer["direction_state"] is None
    assert layer["reported_direction_state"] == "BUY"
    assert layer["direction_qualified"] is False


def test_manual_ssl_note_verifies_prices_without_upgrading_label_or_status(helper):
    payload = structure()
    notes = notes_for_sweep(payload)
    result = helper.prepare_chart_data(payload, notes=notes)
    item = result["ict"]["notes"][0]
    assert item["data_verified"] is True
    assert item["classification"] == "manual"
    assert item["label_is_interpretive"] is True
    assert item["liquidity_meaning_verified"] is False
    assert item["final_status_upgraded"] is False
    assert result["ict"]["final_status"] == "not_assessed"


@pytest.mark.parametrize("damage", ["reference", "extreme", "close", "future_mss", "mss_price", "symbol"])
def test_manual_note_cannot_fabricate_anchors_or_confirmation(helper, damage):
    payload = structure()
    notes = notes_for_sweep(payload)
    item = notes["items"][0]
    if damage == "reference":
        item["reference_price"] += .1
    elif damage == "extreme":
        item["extreme"] -= .1
    elif damage == "close":
        item["close"] += .1
    elif damage == "future_mss":
        item["mss_reference_date"] = payload["as_of"]
    elif damage == "mss_price":
        item["mss_reference_price"] += .1
    else:
        notes["symbol"] = "999999.SH"
    with pytest.raises(ValueError):
        helper.prepare_chart_data(payload, notes=notes)


def test_level_note_requires_an_actual_named_ohlc_field(helper):
    payload = structure()
    bar = payload["frequencies"]["日线"]["bars"][-1]
    notes = {"schema": "kcpredict_chart_notes_v1", "symbol": payload["symbol"], "as_of": payload["as_of"],
             "items": [{"type": "level", "date": bar["time"], "field": "close", "price": bar["close"]}]}
    assert helper.prepare_chart_data(payload, notes=notes)["ict"]["notes"][0]["data_verified"] is True
    notes["items"][0]["price"] += 10
    with pytest.raises(ValueError, match="note level"):
        helper.prepare_chart_data(payload, notes=notes)


def test_manual_note_cannot_supply_its_own_verified_status(helper):
    payload = structure()
    notes = notes_for_sweep(payload)
    notes["items"][0]["final_status"] = "confirmed"
    with pytest.raises(ValueError, match="verification claims"):
        helper.prepare_chart_data(payload, notes=notes)


def test_helper_never_mutates_any_input(helper):
    payload = structure()
    prediction = forecast(payload)
    notes = notes_for_sweep(payload)
    # Rebuild forecast because the manual fixture changes a historical close.
    before = copy.deepcopy((payload, prediction, notes))
    helper.prepare_chart_data(payload, prediction, notes=notes)
    assert (payload, prediction, notes) == before
