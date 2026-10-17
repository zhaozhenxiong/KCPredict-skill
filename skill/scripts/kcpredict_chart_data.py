#!/usr/bin/env python3
"""Prepare auditable chart data without a plotting, numerical or model runtime.

Only real, closed daily bars feed history and ICT. A forecast is a separate
validated layer, including failed quality results which remain failed. This
module never downloads data, runs inference, changes source qualifications, or
turns manual annotations into strategy confirmation.
"""
from __future__ import annotations

import calendar
import copy
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kcpredict_ict as bridge
import ict_scan


FEATURES = ("open", "high", "low", "close", "volume", "amount")


def _stamp(value: Any, label: str):
    return bridge._timestamp(value, label)


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    return bridge._numeric(value, label, positive=positive)


def _finite_optional(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite numeric data") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite numeric data")
    return result


def _same(left: Any, right: Any, label: str) -> None:
    try:
        agrees = math.isfinite(float(left)) and math.isfinite(float(right)) and math.isclose(float(left), float(right), rel_tol=1e-7, abs_tol=1e-7)
    except (TypeError, ValueError, OverflowError):
        agrees = False
    if not agrees:
        raise ValueError(f"{label} disagrees with source data")


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _months_before(day: date, months: int) -> date:
    number = day.year * 12 + day.month - 1 - months
    year, month = divmod(number, 12)
    month += 1
    if year < 1:
        raise ValueError("months moves the chart before year 1")
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _available(item: dict, cutoff, *, require_clock: bool = True) -> bool:
    clocks = [item[key] for key in ("available_at", "snapshot_at", "observed_at", "confirmed_at",
                                   "first_available_at", "first_confirmed_at") if item.get(key) is not None]
    if not clocks and item.get("date") is not None:
        clocks = [item["date"]]
    return (bool(clocks) or not require_clock) and all(_stamp(value, "evidence clock") <= cutoff for value in clocks)


def _history(root: dict, analysis: dict) -> tuple[list[dict], Any, dict]:
    cutoffs = {"as_of": _stamp(analysis.get("as_of"), "analysis.as_of"),
               "data_end": _stamp(analysis.get("data_end"), "analysis.data_end")}
    if root is not analysis and root.get("data_end") is not None:
        cutoffs["outer_data_end"] = _stamp(root["data_end"], "data_end")
    cutoff = min(cutoffs.values())
    frequencies = analysis.get("frequencies")
    if not isinstance(frequencies, dict) or not isinstance(frequencies.get("日线"), dict):
        raise ValueError("analysis.frequencies.日线 is required")
    rows = frequencies["日线"].get("bars")
    if not isinstance(rows, list):
        raise ValueError("daily bars must be an array")
    bars, excluded = [], {"not_closed": 0, "after_cutoff": 0, "observed_after_cutoff": 0}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"daily bar {index} must be an object")
        if row.get("is_closed") is not True:
            excluded["not_closed"] += 1
            continue
        stamp = _stamp(row.get("time"), f"daily bar {index} time")
        observed = _stamp(row["observed_at"], "bar observed_at") if row.get("observed_at") else None
        if stamp > cutoff:
            excluded["after_cutoff"] += 1
            continue
        if observed is not None and observed > cutoff:
            excluded["observed_after_cutoff"] += 1
            continue
        bar = {"time": stamp.date().isoformat(), "date": stamp.date().isoformat(),
               "timestamp": stamp.isoformat(), "is_closed": True}
        for field in FEATURES:
            if field in row or field in FEATURES[:4]:
                bar[field] = _number(row.get(field), f"daily bar {index} {field}", positive=field in FEATURES[:4])
        if bar["high"] < max(bar["open"], bar["low"], bar["close"]) or bar["low"] > min(bar["open"], bar["high"], bar["close"]):
            raise ValueError(f"daily bar {index} has inconsistent OHLC geometry")
        if observed is not None:
            bar["observed_at"] = observed.isoformat()
        bars.append(bar)
    times = [bar["time"] for bar in bars]
    if not times or times != sorted(set(times)):
        raise ValueError("eligible closed daily bars must be nonempty, increasing and unique")
    # A complete prior-20 baseline is required. Missing activity stays missing.
    for index, bar in enumerate(bars):
        previous = bars[max(0, index - 20):index]
        volumes = [row.get("volume") for row in previous]
        bar["volume_mean20_prior"] = (sum(volumes) / 20 if len(volumes) == 20 and all(v is not None for v in volumes) else None)
        baseline = bar["volume_mean20_prior"]
        bar["relative_volume20"] = (bar["volume"] / baseline if bar.get("volume") is not None and baseline and baseline > 0 else None)
    return bars, cutoff, {"input_bars": len(rows), "closed_bars": len(bars), "excluded": excluded,
                          "cutoffs": {key: value.isoformat() for key, value in cutoffs.items()}}


def _geometry(rows: Any, kind: str, start: date, cutoff) -> list[dict]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{kind} must be an array")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{kind} items must be objects")
        left, right = _stamp(row.get("start_time"), "geometry start"), _stamp(row.get("end_time"), "geometry end")
        if left > right:
            raise ValueError("geometry start follows end")
        if right.date() < start or right > cutoff or left > cutoff:
            continue
        # An unfinished geometry from a later observation cannot be replayed
        # at an earlier cutoff. Completed historical geometry is explicitly
        # retrospective, including when its confirmation is still in future.
        observed = _stamp(row["observed_at"], "geometry observed_at") if row.get("observed_at") else None
        if observed is not None and observed > cutoff and row.get("is_confirmed") is not True:
            continue
        stamp = row.get("confirmed_at") or row.get("first_confirmed_at")
        confirmed = _stamp(stamp, "geometry confirmation") if stamp else None
        display_confirmed = row.get("is_confirmed") is True and confirmed is not None and confirmed <= cutoff
        entry = copy.deepcopy(row)
        for field in (("start_price", "end_price") if kind == "pens" else ("low", "high")):
            entry[field] = _number(row.get(field), f"{kind} {field}", positive=True)
        if kind == "zones" and entry["low"] > entry["high"]:
            raise ValueError("zone lower boundary exceeds upper boundary")
        entry.update(display_confirmed=display_confirmed, confirmation_time=confirmed.isoformat() if confirmed else None,
                     is_retroactive=confirmed is not None and confirmed > right,
                     unavailable_as_of=confirmed is not None and confirmed > cutoff)
        noun = "中枢" if kind == "zones" else "笔"
        entry["label"] = (f"回溯{noun}；{confirmed.date().isoformat()}确认" if display_confirmed
                          else f"回溯{noun}；截止时未确认")
        result.append(entry)
    return result


def _evidence(analysis: dict, cutoff, last: dict) -> tuple[dict, dict]:
    evidence = analysis.get("source_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    snapshots = list(evidence.get("evaluations", []))
    if isinstance(evidence.get("latest"), dict):
        snapshots.append(evidence["latest"])
    snapshots = [row for row in snapshots if isinstance(row, dict) and _available(row, cutoff)
                 and (not row.get("date") or _stamp(row["date"], "snapshot date").date().isoformat() <= last["time"])]
    snapshots.sort(key=lambda row: _stamp(row.get("available_at", row.get("date")), "snapshot available_at"))

    def select(name: str) -> dict:
        choices = [row[name] for row in snapshots if isinstance(row.get(name), dict) and _available(row[name], cutoff)]
        if not choices:
            return {"status": "unavailable", "direction_state": None, "available_at": None,
                    "direction_qualified": False, "current_state": {}, "support_facts": [],
                    "reason_codes": ["source_evidence_missing"]}
        value = copy.deepcopy(choices[-1])
        value["support_facts"] = [row for row in value.get("support_facts", [])
                                  if isinstance(row, dict) and _available(row, cutoff)]
        value["status"] = (evidence.get("status", "available") if value.get("input_eligible") is True else "unavailable")
        value["direction_qualified"] = value["status"] == "available" and value.get("input_eligible") is True
        if value["status"] == "unavailable":
            value["reported_direction_state"] = value.get("direction_state")
            value["direction_state"] = None
        return value

    structure, volume = select("structure"), select("wyckoff")
    w32 = analysis.get("price_volume")
    w32 = w32 if isinstance(w32, dict) else {}
    rows = list(w32.get("rows", []))
    if isinstance(w32.get("latest"), dict):
        rows.append(w32["latest"])
    rows = [row for row in rows if isinstance(row, dict) and row.get("date")
            and _stamp(row["date"], "W32 date") <= cutoff
            and (not row.get("wyckoff_available_at") or _stamp(row["wyckoff_available_at"], "W32 available_at") <= cutoff)
            and _stamp(row["date"], "W32 date").date().isoformat() <= last["time"]]
    rows.sort(key=lambda row: _stamp(row["date"], "W32 date"))
    volume["w32_latest"] = copy.deepcopy(rows[-1]) if rows else None
    current = volume.get("current_state", {})
    volume["relative_volume20"] = _finite_optional(current.get("relative_volume"), "relative_volume")
    if volume["relative_volume20"] is None:
        volume["relative_volume20"] = last.get("relative_volume20")
    latest = volume["w32_latest"] or {}
    volume["legacy_state_is_fresh"] = (latest.get("wyckoff_event") not in (None, "none", "unknown")
                                        and latest.get("w_event_age") == 0
                                        and _stamp(latest["date"], "W32 date").date().isoformat() == last["time"])
    volume["warnings"] = []
    if latest.get("wyckoff_state") and not volume["legacy_state_is_fresh"]:
        volume["warnings"].append("W32 state is carried history, not a new demand/supply event on the chart cutoff.")
    return structure, volume


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    offset = (len(ordered) - 1) * probability
    low = math.floor(offset)
    high = math.ceil(offset)
    return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)


def _path_valid(path: list[list[Any]]) -> bool:
    try:
        finite = all(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                     for row in path for value in row)
    except OverflowError:
        finite = False
    if not finite:
        return False
    rows = path
    return all(all(value > 0 for value in row[:4]) and all(value >= 0 for value in row[4:])
               and row[1] >= max(row[0], row[2], row[3]) and row[2] <= min(row[0], row[1], row[3]) for row in rows)


def _prediction(payload: Any, root: dict, meta: dict, last: dict, bars: list[dict]) -> dict:
    empty = {"forecast": [], "dates": [], "samples": [], "valid_mask": [], "quality": {}, "summary": None,
             "input": {}, "parameters": {}, "raw_samples_available": False, "validation": {}, "warnings": []}
    if payload is None:
        return {**empty, "status": "not_executed", "reason": "Prediction was not requested."}
    if not isinstance(payload, dict):
        raise ValueError("prediction payload must be an object")
    if payload.get("schema") == "kcpredict_prediction_unavailable_v1":
        if payload.get("status") not in {"unavailable", "not_executed", "not_requested"}:
            raise ValueError("unsupported unavailable prediction status")
        if payload.get("symbol") != meta["symbol"]:
            raise ValueError("unavailable prediction symbol disagrees with history")
        for key in ("as_of", "data_end"):
            if payload.get(key) is not None and _stamp(payload[key], f"prediction {key}").date().isoformat() != last["time"]:
                raise ValueError("unavailable prediction cutoff disagrees with history")
        return {**empty, "status": "not_executed" if payload["status"] in {"not_executed", "not_requested"} else "unavailable",
                "reason": payload.get("reason", "Prediction is unavailable."),
                "reason_codes": copy.deepcopy(payload.get("reason_codes", [])),
                "parameters": copy.deepcopy(payload.get("parameters", {})), "command": copy.deepcopy(payload.get("command", {}))}
    pred_root = payload
    if payload.get("schema") == "kcpredict_research_v1":
        if payload.get("symbol") != meta["symbol"]:
            raise ValueError("prediction research symbol disagrees with history")
        payload = payload.get("prediction")
    if not isinstance(payload, dict) or payload.get("schema") != "kronos_forecast_v1":
        raise ValueError("prediction requires kronos_forecast_v1 or the unavailable-result contract")
    if payload.get("status") != "completed":
        raise ValueError("native forecast status must be completed; failures require the unavailable-result contract")
    if payload.get("symbol") != meta["symbol"]:
        raise ValueError("prediction symbol disagrees with history")
    input_info = payload.get("input")
    if not isinstance(input_info, dict) or input_info.get("symbol") != meta["symbol"]:
        raise ValueError("prediction input symbol is required and must match history")
    for value, label in ((payload.get("as_of"), "prediction as_of"), (input_info.get("as_of"), "input as_of"),
                         (input_info.get("history_end"), "input history_end")):
        if _stamp(value, label).date().isoformat() != last["time"]:
            raise ValueError(f"{label} disagrees with actual last closed bar")
    _same(_number(input_info.get("last_close"), "prediction last_close", positive=True), last["close"], "prediction last_close")
    if input_info.get("lookback") is not None:
        lookback = input_info["lookback"]
        if isinstance(lookback, bool) or not isinstance(lookback, int) or not 1 <= lookback <= len(bars):
            raise ValueError("prediction lookback is incompatible with actual closed history")
        if input_info.get("rows") is not None and input_info["rows"] != lookback:
            raise ValueError("prediction input rows disagree with lookback")
        if input_info.get("history_start") and _stamp(input_info["history_start"], "input history_start").date().isoformat() != bars[-lookback]["time"]:
            raise ValueError("prediction input history_start disagrees with actual closed history")
    if input_info.get("price_basis") and meta.get("price_basis") and input_info["price_basis"] != meta["price_basis"]:
        raise ValueError("prediction price basis disagrees with history")
    left_provenance, right_provenance = root.get("data_provenance"), pred_root.get("data_provenance")
    left_sources = left_provenance.get("source_files") if isinstance(left_provenance, dict) else None
    right_sources = right_provenance.get("source_files") if isinstance(right_provenance, dict) else None
    if left_sources and right_sources and left_sources != right_sources:
        raise ValueError("prediction source file fingerprints disagree with history")
    quality = payload.get("quality")
    if not isinstance(quality, dict) or not isinstance(quality.get("prediction_usable"), bool):
        raise ValueError("prediction quality must retain an explicit prediction_usable boolean")
    quality = copy.deepcopy(quality)
    horizon = input_info.get("horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError("prediction input horizon must be a positive integer")
    forecast = payload.get("forecast")
    if not isinstance(forecast, list):
        raise ValueError("prediction forecast must be an array")
    if forecast and len(forecast) != horizon:
        raise ValueError("forecast length disagrees with input horizon")
    dates, clean_forecast = [], []
    for row in forecast:
        if not isinstance(row, dict):
            raise ValueError("forecast rows must be objects")
        stamp = _stamp(row.get("date"), "forecast date")
        if stamp.date().isoformat() <= last["time"]:
            raise ValueError("forecast dates must follow actual history")
        clean = copy.deepcopy(row)
        clean["date"] = stamp.date().isoformat()
        for field in (*FEATURES, "close_p10", "close_p90", "close_mean"):
            if field in row or field in ("close", "close_p10", "close_p90"):
                clean[field] = _number(row.get(field), "forecast " + field, positive=field not in ("volume", "amount"))
        if not clean["close_p10"] <= clean["close"] <= clean["close_p90"]:
            raise ValueError("forecast close quantiles are not ordered")
        if all(key in clean for key in FEATURES[:4]) and (
                clean["high"] < max(clean["open"], clean["low"], clean["close"])
                or clean["low"] > min(clean["open"], clean["high"], clean["close"])):
            raise ValueError("forecast median OHLC geometry is inconsistent")
        dates.append(clean["date"])
        clean_forecast.append(clean)
    if dates != sorted(set(dates)):
        raise ValueError("forecast dates must be increasing and unique")
    identity = payload.get("identity", {})
    if not isinstance(identity, dict) or not isinstance(identity.get("parameters", {}), dict):
        raise ValueError("prediction identity/parameters must be objects")
    if identity.get("future_dates") is not None and dates and identity["future_dates"] != dates:
        raise ValueError("forecast dates disagree with the frozen identity")
    raw = payload.get("samples")
    raw_available = raw is not None
    mask = quality.get("valid_mask")
    if not isinstance(mask, list) or not all(isinstance(value, bool) for value in mask):
        raise ValueError("prediction quality valid_mask must be a boolean array")
    if raw_available:
        if not isinstance(raw, list) or not raw:
            raise ValueError("raw samples must be a nonempty array")
        if any(not isinstance(path, list) or len(path) != horizon or any(not isinstance(row, list) or len(row) != 6 for row in path) for path in raw):
            raise ValueError("raw samples require sample × horizon × 6 OHLCVA shape")
        computed_mask = [_path_valid(path) for path in raw]
        if mask != computed_mask:
            raise ValueError("raw sample validity disagrees with quality.valid_mask")
    count = sum(mask)
    requested = len(mask)
    if requested < 1:
        raise ValueError("quality valid_mask must describe at least one requested sample")
    for key, expected in (("requested_samples", requested), ("valid_samples", count), ("invalid_samples", requested - count)):
        if isinstance(quality.get(key), bool) or not isinstance(quality.get(key), int) or quality[key] != expected:
            raise ValueError(f"quality.{key} disagrees with valid_mask")
    for key, expected in (("samples", requested), ("horizon", horizon)):
        if identity.get("parameters", {}).get(key) is not None and identity["parameters"][key] != expected:
            raise ValueError(f"prediction parameters.{key} disagrees with actual sample shape")
    _same(_number(quality.get("valid_fraction"), "valid_fraction"), count / requested, "valid_fraction")
    minimum_count = quality.get("minimum_valid_samples", 8)
    minimum_fraction = quality.get("minimum_valid_fraction", .8)
    if isinstance(minimum_count, bool) or not isinstance(minimum_count, int) or minimum_count < 1:
        raise ValueError("minimum_valid_samples must be positive")
    minimum_fraction = _number(minimum_fraction, "minimum_valid_fraction", positive=True)
    if minimum_fraction > 1:
        raise ValueError("minimum_valid_fraction cannot exceed one")
    numerical_gate = count >= minimum_count and count / requested >= minimum_fraction
    explicit_gate_failure = any(quality.get(key) is False for key in ("calendar_verified", "input_contract_verified", "source_verified"))
    if quality["prediction_usable"] and (not numerical_gate or explicit_gate_failure or not forecast):
        raise ValueError("prediction_usable=true contradicts its quality gates")
    if bool(count) != bool(forecast):
        raise ValueError("forecast presence disagrees with valid sample count")
    valid_paths = [path for path, valid in zip(raw or [], mask) if valid]
    if raw_available and count:
        for index, row in enumerate(clean_forecast):
            for column, field in enumerate(FEATURES):
                if field in row:
                    _same(row[field], _quantile([path[index][column] for path in valid_paths], .5), "forecast " + field)
            closes = [path[index][3] for path in valid_paths]
            for field, expected in (("close_p10", _quantile(closes, .1)), ("close_p90", _quantile(closes, .9)),
                                    ("close_mean", sum(closes) / count)):
                if field in row:
                    _same(row[field], expected, "forecast " + field)
    summary = copy.deepcopy(payload.get("summary"))
    if summary is not None:
        if not isinstance(summary, dict):
            raise ValueError("prediction summary must be an object or null")
        for key, value in summary.items():
            if key not in ("probability_semantics", "interval_semantics", "return_basis") and value is not None:
                _finite_optional(value, "summary " + key)
        if raw_available and count:
            terminal = [path[-1][3] / last["close"] - 1 for path in valid_paths]
            expected = {"terminal_return_p10": _quantile(terminal, .1), "terminal_return_median": _quantile(terminal, .5),
                        "terminal_return_p90": _quantile(terminal, .9), "up_path_fraction": sum(value > 0 for value in terminal) / count,
                        "terminal_return_std": math.sqrt(sum((value - sum(terminal) / count) ** 2 for value in terminal) / count),
                        "worst_path_low_return": min(row[2] for path in valid_paths for row in path) / last["close"] - 1}
            for key, value in expected.items():
                if key in summary:
                    _same(summary[key], value, "summary " + key)
    status = "pass" if quality["prediction_usable"] else "fail"
    warnings = ["Forecast samples and quantiles are model scenarios, never observed ICT evidence."]
    if not raw_available:
        warnings.append("Raw sample paths are absent; forecast quantiles cannot be independently recomputed.")
    if status == "fail":
        warnings.append("Prediction quality failed; plotting the available scenarios does not upgrade it to pass.")
    return {"status": status, "reason": "Prediction quality passed." if status == "pass" else "Prediction quality did not pass.",
            "forecast": clean_forecast, "dates": dates, "samples": copy.deepcopy(valid_paths), "valid_mask": copy.deepcopy(mask),
            "quality": quality, "summary": summary, "input": copy.deepcopy(input_info),
            "parameters": copy.deepcopy(identity.get("parameters", {})), "resource": copy.deepcopy(payload.get("resource", {})),
            "raw_samples_available": raw_available, "validation": {"symbol_as_of_last_close": True,
                "sample_geometry_recomputed": raw_available, "forecast_recomputed_and_compared": raw_available,
                "quantile_basis": "valid_samples_only" if raw_available else "supplied_forecast_without_raw_samples"}, "warnings": warnings}


def _ict(research: dict, analysis: dict, provided: Any, fingerprint: str, bars: list[dict]) -> dict:
    # Prediction bytes never take part in the historical identity or scanner.
    canonical_hash = _hash({key: value for key, value in research.items() if key not in ("prediction", "prediction_calendar_validation")})
    if provided is not None and (not isinstance(provided, dict) or provided.get("schema") != "kcpredict_ict_v1" or provided.get("frequency") != "日线"):
        raise ValueError("provided ICT must be a daily kcpredict_ict_v1 payload")
    supplied_scan = provided.get("scan") if provided else None
    if supplied_scan is not None and not isinstance(supplied_scan, dict):
        raise ValueError("provided ICT scan must be an object or null")
    parameters = (supplied_scan or {}).get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("ICT scan parameters must be an object")
    local = bridge.adapt(research, input_sha256=canonical_hash, direction=parameters.get("direction", "auto"),
                         swing_width=parameters.get("swing_width", 2), minimum_rr=parameters.get("minimum_rr", 2.0))
    validation = {"history_fingerprint": fingerprint, "source": "local_closed_history_adapter"}
    if provided is not None:
        source, expected = provided.get("source", {}), local["source"]
        if not isinstance(source, dict):
            raise ValueError("provided ICT source must be an object")
        for key in ("symbol", "as_of"):
            if source.get(key) != expected.get(key):
                raise ValueError(f"ICT source {key} disagrees with actual history")
        for key in ("data_end", "analysis_data_end", "price_basis", "data_version", "data_range"):
            if expected.get(key) is not None and source.get(key) != expected[key]:
                raise ValueError(f"ICT source {key} disagrees with actual history")
        # A file-byte hash cannot be reconstructed from an already parsed dict.
        # Require a comparable immutable history/version fingerprint instead,
        # or the canonical dictionary hash used by this helper's local bridge.
        identity_matches = (bool(analysis.get("data_version")) and source.get("data_version") == analysis["data_version"]
                            or source.get("history_fingerprint") == fingerprint
                            or source.get("input_sha256") == canonical_hash)
        if not identity_matches:
            raise ValueError("ICT lacks a comparable source data fingerprint")
        if provided.get("selection") != local.get("selection") or provided.get("scan") != local.get("scan"):
            raise ValueError("provided ICT detections disagree with a fresh scan of the same actual closed history")
        local = provided
        validation["source"] = "supplied_payload_verified_against_closed_history"
    scan = local.get("scan") or {}
    gaps = copy.deepcopy(scan.get("recent_fvgs", []))
    for gap in gaps:
        gap["candle_dates"] = [row["time"] for row in bars[gap["start_index"]:gap["end_index"] + 1]]
        gap["formation_time"] = gap["confirmed_time"]
        gap["lifecycle"] = _fvg_lifecycle(gap, bars)
    return {"status": local["status"], "source": copy.deepcopy(local["source"]), "validation": validation,
            "latest_fvg": gaps[-1] if gaps else None,
            "latest_bullish_fvg": next((row for row in reversed(gaps) if row["direction"] == "bullish"), None),
            "latest_bearish_fvg": next((row for row in reversed(gaps) if row["direction"] == "bearish"), None),
            "recent_fvgs": gaps, "latest_candidate": copy.deepcopy(scan.get("latest_candidate")),
            "candidates": copy.deepcopy(scan.get("candidates", [])), "notes": [],
            "final_status": "not_assessed", "warnings": copy.deepcopy(local.get("warnings", []))}


def _fvg_lifecycle(gap: dict, bars: list[dict]) -> dict:
    """Describe observed mitigation without claiming an intrabar sequence."""
    lower, upper = gap["zone"]
    bullish = gap["direction"] == "bullish"
    events = {name: None for name in ("touch", "full_fill", "close_through")}
    after = bars[gap["end_index"] + 1:]
    for index, bar in enumerate(after, gap["end_index"] + 1):
        touches = bar["low"] <= upper and bar["high"] >= lower
        fills = touches and (bar["low"] <= lower if bullish else bar["high"] >= upper)
        closes_through = bar["close"] < lower if bullish else bar["close"] > upper
        for name, happened in (("touch", touches), ("full_fill", fills), ("close_through", closes_through)):
            if happened and events[name] is None:
                events[name] = {"index": index, "date": bar["time"], "time": bar["timestamp"]}
        if closes_through:
            # A later opposite-side revisit does not revive the original FVG.
            break
    status = ("closed_through" if events["close_through"] is not None else "filled" if events["full_fill"] is not None
              else "touched" if events["touch"] is not None else "active")
    result = {"status": status, "is_active": status in ("active", "touched"),
              "evaluated_through": bars[-1]["time"], "observed_bars_after_confirmation": len(after),
              "rule": "After the third closed bar: range overlap is touch; trading the far boundary is full fill; a close beyond the far boundary permanently ends this FVG. No intrabar order is inferred."}
    for name, event in events.items():
        for field in ("date", "time", "index"):
            result[name + "_" + field] = event[field] if event else None
    return result


def _notes(payload: Any, bars: list[dict], meta: dict) -> list[dict]:
    if payload is None:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != "kcpredict_chart_notes_v1":
        raise ValueError("notes require kcpredict_chart_notes_v1")
    if payload.get("symbol") != meta["symbol"] or _stamp(payload.get("as_of"), "notes as_of").date().isoformat() != bars[-1]["time"]:
        raise ValueError("notes symbol/as_of disagrees with actual history")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("notes.items must be an array")
    by_date = {row["time"]: row for row in bars}
    candles, _ = ict_scan.parse_candles({"candles": [{**row, "timestamp": row["timestamp"]} for row in bars]}) if len(bars) >= 10 else ([], {})
    swings = ict_scan.detect_swings(candles, 2) if candles else []
    result = []
    for original in items:
        if not isinstance(original, dict):
            raise ValueError("notes items must be objects")
        item = copy.deepcopy(original)
        allowed = {"type", "date", "reference_date", "reference_price", "extreme", "close", "mss_reference_date",
                   "mss_reference_price", "label", "field", "price"}
        if set(item) - allowed:
            raise ValueError("manual note contains unsupported fields or verification claims")
        day = _stamp(item.get("date"), "note date").date().isoformat()
        if day not in by_date:
            raise ValueError("note date must anchor an eligible closed actual bar")
        current = by_date[day]
        kind = item.get("type")
        if kind == "level":
            field = item.get("field")
            if field not in FEATURES[:4]:
                raise ValueError("level note field must be open/high/low/close")
            _same(_number(item.get("price"), "note price", positive=True), current[field], "note level")
        elif kind in ("ssl_candidate", "bsl_candidate"):
            reference_day = _stamp(item.get("reference_date"), "reference date").date().isoformat()
            if reference_day not in by_date or reference_day >= day:
                raise ValueError("sweep reference must be an earlier eligible closed actual bar")
            long = kind == "ssl_candidate"
            field = "low" if long else "high"
            reference = by_date[reference_day][field]
            _same(_number(item.get("reference_price"), "reference price", positive=True), reference, "sweep reference")
            _same(_number(item.get("extreme"), "sweep extreme", positive=True), current[field], "sweep extreme")
            _same(_number(item.get("close"), "sweep close", positive=True), current["close"], "sweep close")
            if not (current["low"] < reference < current["close"] if long else current["high"] > reference > current["close"]):
                raise ValueError("manual sweep does not penetrate and close back across its reference")
            item["reference_previously_crossed"] = any(
                row["low"] < reference if long else row["high"] > reference
                for row in bars if reference_day < row["time"] < day)
            item["liquidity_meaning_verified"] = False
            if bool(item.get("mss_reference_date")) != (item.get("mss_reference_price") is not None):
                raise ValueError("MSS reference date and price must be supplied together")
            if item.get("mss_reference_date"):
                anchor = _stamp(item["mss_reference_date"], "MSS reference date").date().isoformat()
                if anchor not in by_date or anchor >= day:
                    raise ValueError("MSS reference must precede the manual sweep")
                expected_field = "high" if long else "low"
                _same(_number(item["mss_reference_price"], "MSS reference", positive=True), by_date[anchor][expected_field], "MSS reference")
                matches = [s for s in swings if s["kind"] == expected_field and _stamp(s["time"], "swing time").date().isoformat() == anchor
                           and _stamp(s["confirmed_time"], "swing confirmation").date().isoformat() < day]
                if not matches:
                    raise ValueError("manual MSS anchor was not a confirmed opposing swing before the sweep")
                item["mss_anchor_confirmed_time"] = _stamp(matches[-1]["confirmed_time"], "MSS confirmation").isoformat()
            item["direction"] = "long" if long else "short"
        else:
            raise ValueError("note type must be ssl_candidate, bsl_candidate or level")
        if item.get("label") is not None and not isinstance(item["label"], str):
            raise ValueError("note label must be text")
        item.update(classification="manual", data_verified=True, evidence_status="price_geometry_verified",
                    label_is_interpretive=True, final_status_upgraded=False)
        result.append(item)
    return result


def prepare_chart_data(research: dict, prediction_payload: dict | None = None,
                       ict_payload: dict | None = None, notes: dict | None = None,
                       months: int = 3) -> dict:
    """Validate and separate history, evidence, notes and optional forecast.

    Invalid or incompatible supplied evidence raises ValueError; fields are
    never repaired, imputed, or promoted to pass. Missing optional evidence is
    explicitly unavailable/not_executed in the returned renderer contract.
    """
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError("months must be a positive integer")
    root, analysis = bridge._structure(research)
    symbol = analysis.get("symbol") or root.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("structure symbol is required")
    bars, cutoff, selection = _history(root, analysis)
    requested_start = _months_before(date.fromisoformat(bars[-1]["time"]), months)
    visible = [row for row in bars if date.fromisoformat(row["time"]) >= requested_start]
    prior = [row for row in bars if date.fromisoformat(row["time"]) < requested_start]
    fingerprint = _hash([{key: row.get(key) for key in ("time", *FEATURES)} for row in bars])
    meta = {"symbol": symbol, "requested_end": root.get("requested_end", analysis.get("as_of")),
            "as_of": analysis.get("as_of"), "data_end": bars[-1]["time"], "cutoff": cutoff.isoformat(),
            "months": months, "timezone": "Asia/Shanghai", "price_basis": analysis.get("price_basis"),
            "history_fingerprint": fingerprint, "data_version": analysis.get("data_version"),
            "data_provenance": copy.deepcopy(root.get("data_provenance")), "selection": selection, "warnings": []}
    daily = analysis["frequencies"]["日线"]
    structure, volume = _evidence(analysis, cutoff, bars[-1])
    if prediction_payload is None and isinstance(root.get("prediction"), dict):
        prediction_payload = root
    prediction = _prediction(prediction_payload, root, meta, bars[-1], bars)
    ict = _ict(research, analysis, ict_payload, fingerprint, bars)
    ict["notes"] = _notes(notes, bars, meta)
    return {"schema": "kcpredict_chart_data_v1", "meta": meta,
            "history": {"bars": copy.deepcopy(visible), "date_start": visible[0]["time"], "date_end": visible[-1]["time"],
                        "requested_start": requested_start.isoformat(), "warmup_bars": len(prior[-20:]),
                        "warmup_history": copy.deepcopy(prior[-20:])},
            "full_history": copy.deepcopy(bars),
            "pens": _geometry(daily.get("pens"), "pens", requested_start, cutoff),
            "zones": _geometry(daily.get("zones"), "zones", requested_start, cutoff),
            "structure": structure, "price_volume": volume, "ict": ict, "prediction": prediction,
            "quality": {"prediction_status": prediction["status"], "prediction_usable": prediction.get("quality", {}).get("prediction_usable"),
                        "structure_status": structure["status"], "price_volume_status": volume["status"], "ict_status": ict["status"],
                        "warnings": [*meta["warnings"], *volume.get("warnings", []), *prediction["warnings"]]}}
