#!/usr/bin/env python3
"""Create an offline, continuous-timeline HTML view from reviewed chart evidence."""
from __future__ import annotations

import argparse
import copy
from datetime import date
import hashlib
import json
import math
from pathlib import Path


def build_payload(data: dict) -> dict:
    if data.get("schema") != "kcpredict_chart_data_v1":
        raise ValueError("Expected reviewed kcpredict_chart_data_v1 evidence")
    bars = data.get("history", {}).get("bars") or []
    if not bars:
        raise ValueError("No closed historical bars")
    previous = None
    for bar in bars:
        day = date.fromisoformat(str(bar["time"])[:10])
        if previous is not None and day <= previous:
            raise ValueError("Historical dates must be strictly increasing")
        if bar.get("is_closed") is not True:
            raise ValueError("Only closed historical bars can be displayed")
        for field in ("open", "high", "low", "close"):
            value = bar.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Historical prices must be finite and positive")
        previous = day
    meta = data["meta"]
    if str(meta["data_end"])[:10] != str(bars[-1]["time"])[:10]:
        raise ValueError("Historical last bar and data_end disagree")
    prediction = data["prediction"]
    if prediction["status"] not in ("pass", "fail", "unavailable", "not_executed"):
        raise ValueError("Unknown prediction quality state")
    rows = prediction.get("forecast") or []
    dates = prediction.get("dates") or []
    if [str(row["date"])[:10] for row in rows] != [str(day)[:10] for day in dates]:
        raise ValueError("Forecast dates and reviewed calendar dates disagree")
    for row in rows:
        day = date.fromisoformat(str(row["date"])[:10])
        if day <= previous:
            raise ValueError("Forecast dates must follow the last closed historical bar")
        for field in ("close", "close_p10", "close_p90"):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Forecast prices must be finite and positive")
        if not row["close_p10"] <= row["close"] <= row["close_p90"]:
            raise ValueError("Forecast quantiles are not ordered")
        previous = day
    quality = prediction.get("quality") or {}
    if prediction["status"] == "pass" and quality.get("prediction_usable") is not True:
        raise ValueError("Passing quality requires the reviewed usability flag")
    if prediction["status"] == "pass" and not rows:
        raise ValueError("Passing quality requires forecast rows")
    if prediction["status"] != "pass" and quality.get("prediction_usable") is True:
        raise ValueError("A non-passing state cannot have a passing usability flag")
    if prediction["status"] in ("unavailable", "not_executed") and rows:
        raise ValueError("Unavailable or unexecuted predictions cannot contain forecast rows")
    for path in prediction.get("samples") or []:
        if len(path) != len(rows) or any(len(row) < 4 or not math.isfinite(row[3]) or row[3] <= 0 for row in path):
            raise ValueError("Sample paths do not match reviewed forecast geometry")
    # Keep relevant evidence and hashes; do not embed private absolute source paths.
    result = {"schema":"kcpredict_html_data_v1", "meta":{key:meta.get(key) for key in
        ("symbol", "requested_end", "as_of", "data_end", "months", "timezone", "price_basis", "history_fingerprint", "data_version")},
        "bars":copy.deepcopy(bars), "pens":copy.deepcopy(data.get("pens", [])),
        "zones":copy.deepcopy(data.get("zones", [])), "prediction":copy.deepcopy(prediction),
        "quality":copy.deepcopy(data.get("quality", {})), "ict":copy.deepcopy(data.get("ict", {})),
        "structure":copy.deepcopy(data.get("structure", {})), "price_volume":copy.deepcopy(data.get("price_volume", {})),
        "provenance":{key:copy.deepcopy((meta.get("data_provenance") or {}).get(key)) for key in
            ("price_basis", "volume_unit", "amount_unit", "provenance", "schema", "externally_verified")},
        "warnings":copy.deepcopy(meta.get("warnings") or []),
        "source_hashes":[{"name":Path(name).name, "sha256":sha} for name,sha in data.get("source_files", {}).items()]}
    # A source snapshot may retain years of support facts. Only the displayed window's
    # facts belong in this standalone view; current-state qualification stays intact.
    first = str(bars[0]["time"])[:10]
    facts = result["structure"].get("support_facts") or []
    result["structure"]["support_facts"] = [fact for fact in facts if
        first <= str(fact.get("first_confirmed_at") or fact.get("confirmed_at") or fact.get("date") or "")[:10] <= str(meta["data_end"])[:10]]
    result["prediction"].pop("command", None)
    raw_source = result["ict"].get("source") or {}
    result["ict"]["source"] = {key:copy.deepcopy(raw_source[key]) for key in
        ("input_schema", "input_sha256", "symbol", "data_end", "as_of", "price_basis", "data_range", "data_version") if key in raw_source}
    return result


def render_html(data: dict, output: Path) -> dict:
    payload = build_payload(data)
    template = Path(__file__).resolve().parents[1]/"assets"/"composite.html"
    text = template.read_text(encoding="utf-8")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON script contents are data even when a user's note contains </script>.
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if text.count("__DATA_JSON__") != 1:
        raise ValueError("HTML template must contain exactly one data insertion point")
    text = text.replace("__DATA_JSON__", encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return {"schema":"kcpredict_html_artifact_v1", "symbol":payload["meta"]["symbol"],
        "data_end":payload["meta"]["data_end"], "prediction_status":payload["prediction"]["status"],
        "html":str(output.resolve()), "bytes":output.stat().st_size,
        "sha256":hashlib.sha256(output.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Reviewed chart-data.json; no model inference")
    parser.add_argument("--output", type=Path, required=True, help="New standalone HTML file; existing files are protected")
    args = parser.parse_args()
    try:
        result = render_html(json.loads(args.data.read_text(encoding="utf-8-sig")), args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"HTML report failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
