"""Carry original display indicators and source clocks into the portable report."""
from __future__ import annotations

import copy


def prepare_layers(analysis, start, cutoff, daily_bars):
    # Reuse the report's timestamp and geometry checks; do not recalculate signals.
    from kcpredict_chart_data import _available, _geometry, _number, _stamp

    def in_window(row, field="time"):
        value = row.get(field)
        return bool(value) and start <= _stamp(value, field).date() <= cutoff.date()

    def known(row):
        return _available(row, cutoff, require_clock=False)

    views = {}
    for name, source in analysis["frequencies"].items():
        if name not in ("日线", "周线", "月线"):
            continue
        bars = copy.deepcopy(daily_bars) if name == "日线" else [
            copy.deepcopy(row) for row in source.get("bars", [])
            if in_window(row) and known(row)]
        for row in bars:
            for field in ("open", "high", "low", "close"):
                row[field] = _number(row.get(field), field, positive=True)
            if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
                raise ValueError("Invalid higher-period OHLC geometry")
        times = [row["time"] for row in bars]
        if times != sorted(set(times)):
            raise ValueError("Period dates must be increasing and unique")
        indicators = source.get("indicators") or {}
        rows = [copy.deepcopy(row) for row in indicators.get("rows", [])
                if row.get("time") in times and known(row)]
        profiles = {day: copy.deepcopy(profile) for day, profile in
                    (indicators.get("chip_profiles") or {}).items()
                    if day in times and profile.get("as_of")
                    and _stamp(profile["as_of"], "chip as_of").date() <= _stamp(day, "chip day").date()}
        views[name] = {"bars": bars,
            "pens": _geometry(source.get("pens"), "pens", start, cutoff),
            "zones": _geometry(source.get("zones"), "zones", start, cutoff),
            "fractals": [copy.deepcopy(row) for row in source.get("fractals", []) if in_window(row) and known(row)],
            "divergences": [copy.deepcopy(row) for row in source.get("divergences", []) if in_window(row) and known(row)],
            "indicators": {"rows": rows, "chip_profiles": profiles,
                           "chip_method": indicators.get("chip_method")}}
    source = analysis.get("source_evidence") or {}
    events = [copy.deepcopy(row) for row in source.get("events", [])
              if in_window(row, "date") and known(row)]
    volume_rows = [copy.deepcopy(row) for row in (analysis.get("price_volume") or {}).get("rows", [])
                   if in_window(row, "date") and known(row)
                   and (not row.get("wyckoff_available_at") or
                        _stamp(row["wyckoff_available_at"], "wyckoff clock") <= cutoff)]
    return {"frequencies": views, "source_events": events, "volume_rows": volume_rows}
