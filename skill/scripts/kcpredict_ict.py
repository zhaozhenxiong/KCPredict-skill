#!/usr/bin/env python3
"""Adapt closed KCPredict history to the bundled mechanical ICT scanner.

This standard-library-only bridge never reads a forecast or grants a final
trade status. It works independently of the installed KCPredict Python package.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, time
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

# The sibling scanner is part of the portable skill, including under python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ict_scan


SHANGHAI = ZoneInfo("Asia/Shanghai")
FREQUENCIES = ("日线", "周线", "月线")
SESSION = "not_applicable_daily_or_higher"


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an ISO date or timestamp")
    text = value.strip()
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(day, time(15), tzinfo=SHANGHAI)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _numeric(value: Any, label: str, *, positive: bool) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite numeric data") from exc
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'} and finite")
    return number


def _structure(payload: Any) -> tuple[dict, dict]:
    if not isinstance(payload, dict):
        raise ValueError("input must be a KCPredict JSON object")
    schema = payload.get("schema")
    if schema == "kcpredict_research_v1":
        analysis = payload.get("analysis")
        if not isinstance(analysis, dict) or analysis.get("schema") != "kcpredict_structure_v1":
            raise ValueError("research input requires a kcpredict_structure_v1 analysis")
        if payload.get("symbol") and analysis.get("symbol") and payload["symbol"] != analysis["symbol"]:
            raise ValueError("research and analysis symbols disagree")
        return payload, analysis
    if schema == "kcpredict_structure_v1":
        return payload, payload
    raise ValueError("input schema must be kcpredict_research_v1 or kcpredict_structure_v1")


def _exchange_context(value: Any) -> Any:
    """Replace intraday NY labels without changing the scanner's mechanical gates."""
    if isinstance(value, list):
        return [_exchange_context(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    timestamp_keys = {"time", "first_candle", "last_candle", "break_time", "retest_time", "crossed_time",
                      "confirmed_at", "available_at", "observed_at", "formation_time", "confirmed_time",
                      "ce_touch_time", "gate_ready_time", "target_reached_time", "fvg_failure_time",
                      "ambiguous_time", "last_excluded_open_candle"}
    for key, item in value.items():
        if key == "ny_time":
            if item is not None:
                result["exchange_time"] = _timestamp(item, key).isoformat()
        elif key == "session":
            result[key] = SESSION
        elif key == "timezone_for_sessions":
            result[key] = "Asia/Shanghai"
        elif key in timestamp_keys and isinstance(item, str):
            result[key] = _timestamp(item, key).isoformat()
        else:
            result[key] = _exchange_context(item)
    return result


def adapt(payload: Any, *, input_sha256: str, frequency: str = "日线", direction: str = "auto",
          swing_width: int = 2, minimum_rr: float = 2.0) -> dict:
    """Return auditable candidates from actual, closed historical bars only."""
    if frequency not in FREQUENCIES:
        raise ValueError("frequency must be 日线, 周线, or 月线")
    if direction not in {"auto", "long", "short"}:
        raise ValueError("direction must be auto, long, or short")
    if isinstance(swing_width, bool) or not isinstance(swing_width, int) or swing_width < 1:
        raise ValueError("swing-width must be a positive integer")
    if isinstance(minimum_rr, bool) or not math.isfinite(minimum_rr) or minimum_rr <= 0:
        raise ValueError("minimum-rr must be positive and finite")
    root, analysis = _structure(payload)
    cutoffs = {"as_of": _timestamp(analysis.get("as_of"), "analysis.as_of"),
               "data_end": _timestamp(analysis.get("data_end"), "analysis.data_end")}
    if root is not analysis and root.get("data_end") is not None:
        cutoffs["data_end"] = min(cutoffs["data_end"], _timestamp(root["data_end"], "data_end"))
    frequencies = analysis.get("frequencies")
    if not isinstance(frequencies, dict):
        raise ValueError("analysis.frequencies must be an object")
    selected = frequencies.get(frequency)
    if selected is None:
        rows = []
    elif not isinstance(selected, dict) or not isinstance(selected.get("bars"), list):
        raise ValueError("selected frequency must contain a bars array")
    else:
        rows = selected["bars"]

    reasons = {"not_closed": 0, "bar_after_as_of": 0, "bar_after_data_end": 0,
               "observed_after_as_of": 0, "observed_after_data_end": 0}
    candles = []
    excluded = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"bar {index} must be an object")
        # Missing, numeric or truthy closure flags cannot admit a forming bucket.
        if row.get("is_closed") is not True:
            reasons["not_closed"] += 1
            excluded += 1
            continue
        stamp = _timestamp(row.get("time"), f"bar {index} time")
        observed = _timestamp(row["observed_at"], f"bar {index} observed_at") if row.get("observed_at") is not None else None
        failed = []
        for name, cutoff in cutoffs.items():
            if stamp > cutoff:
                failed.append("bar_after_" + name)
            if observed is not None and observed > cutoff:
                failed.append("observed_after_" + name)
        if failed:
            for reason in failed:
                reasons[reason] += 1
            excluded += 1
            continue
        candle = {"timestamp": stamp.isoformat(), "is_closed": True}
        candle.update({key: _numeric(row.get(key), f"bar {index} {key}", positive=True)
                       for key in ("open", "high", "low", "close")})
        if candle["low"] > min(candle["open"], candle["close"]) or candle["high"] < max(candle["open"], candle["close"]):
            raise ValueError(f"bar {index} has inconsistent OHLC geometry")
        if "volume" in row:
            candle["volume"] = _numeric(row["volume"], f"bar {index} volume", positive=False)
        candles.append(candle)
    stamps = [row["timestamp"] for row in candles]
    if stamps != sorted(set(stamps)):
        raise ValueError("selected closed bars must have strictly increasing, unique timestamps")

    source = {"input_schema": root["schema"], "input_sha256": input_sha256}
    for key in ("symbol", "requested_end", "data_end", "data_provenance", "calendar_identity"):
        if key in root:
            source[key] = copy.deepcopy(root[key])
    for key in ("symbol", "as_of", "data_end", "price_basis", "data_range", "data_version", "input_quality", "calendar"):
        if key in analysis:
            source[("analysis_data_end" if key == "data_end" and root is not analysis else key)] = copy.deepcopy(analysis[key])
    if "price_basis" not in source and isinstance(root.get("data_provenance"), dict):
        source["price_basis"] = root["data_provenance"].get("price_basis", "unverified")
    for field, keys in (("source_evidence", ("status", "semantics")),
                        ("price_volume", ("input_eligible", "reason_codes"))):
        upstream = analysis.get(field)
        if isinstance(upstream, dict):
            source[field] = {key: copy.deepcopy(upstream[key]) for key in keys if key in upstream}
    result = {
        "schema": "kcpredict_ict_v1", "status": "completed", "source": source,
        "frequency": frequency, "timezone": "Asia/Shanghai", "session": SESSION,
        "selection": {"input_bars": len(rows), "eligible_closed_bars": len(candles),
                      "excluded_bars": excluded, "exclusion_reasons": reasons,
                      "exclusion_reason_counts_may_overlap": True,
                      "cutoffs": {key: value.isoformat() for key, value in cutoffs.items()},
                      "first_bar": candles[0]["timestamp"] if candles else None,
                      "last_bar": candles[-1]["timestamp"] if candles else None},
        "final_status": "not_assessed", "final_status_requires_manual_context": True,
        "qualification": {"scope": "mechanical_research_only", "formal_qualified": False,
                          "position_or_order_generated": False,
                          "unverified_context": {key: "unverified" for key in (
                              "net_rr", "fees_spread_slippage", "higher_timeframe_narrative",
                              "event_calendar", "corporate_actions", "short_executability")}},
        "warnings": [
            "Scan statuses are mechanical evidence; final status requires manual context.",
            "Daily and higher KCPredict bars do not support New York intraday session filters.",
            "Gross R:R excludes fees, spread and slippage; net R:R and execution are unverified.",
            "ICT labels are price-action hypotheses, not proof of institutional activity.",
        ],
    }
    if len(candles) < 10:
        result.update(status="unavailable", reason_codes=["insufficient_closed_history"],
                      reason="At least 10 eligible closed historical bars are required.", scan=None)
        return result
    parsed, meta = ict_scan.parse_candles({"candles": candles, "meta": {
        "symbol": source.get("symbol"), "frequency": frequency, "session": SESSION,
        "exchange_timezone": "Asia/Shanghai", "historical_input_only": True,
    }})
    result["scan"] = _exchange_context(ict_scan.scan(parsed, meta, direction=direction,
        swing_width=swing_width, minimum_rr=minimum_rr))
    result["scan"]["meta"].update(timezone_for_sessions="Asia/Shanghai", session=SESSION)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="KCPredict research/structure JSON path, or - for stdin")
    parser.add_argument("--frequency", choices=FREQUENCIES, default="日线")
    parser.add_argument("--direction", choices=("auto", "long", "short"), default="auto")
    parser.add_argument("--swing-width", type=int, default=2)
    parser.add_argument("--minimum-rr", type=float, default=2.0)
    parser.add_argument("--output", type=Path, help="Optional new JSON file; existing files are never overwritten")
    options = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read() if options.input == "-" else Path(options.input).read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        result = adapt(payload, input_sha256=hashlib.sha256(raw).hexdigest(), frequency=options.frequency,
                       direction=options.direction, swing_width=options.swing_width, minimum_rr=options.minimum_rr)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if options.output is not None:
            with options.output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 2 if result["status"] == "unavailable" else 0
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
