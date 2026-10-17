"""Independent public API boundaries over the real frozen native engine."""
import copy
import json

import numpy as np
import pandas as pd
import pytest

from kcpredict.structure import analyze


@pytest.fixture(scope="module")
def history():
    count = 260
    rng = np.random.default_rng(17)
    close = 20 * np.exp(np.cumsum(rng.normal(.0006, .025, count)))
    volume = rng.uniform(600_000, 2_000_000, count)
    result = pd.DataFrame({"symbol": "000001.SZ", "date": pd.bdate_range("2020-01-02", periods=count),
        "open": close * .998, "high": close * 1.009, "low": close * .99, "close": close,
        "volume": volume, "amount": close * volume,
        "source": "sina_unadjusted_v1", "has_trade_price": 1})
    result.attrs.update(price_basis="unadjusted", volume_unit="shares", amount_unit="CNY")
    return result


def calendar(history):
    return history.date.dt.strftime("%Y-%m-%d").tolist()


def test_analyze_is_strict_json_and_does_not_mutate_input(history):
    original = copy.deepcopy(history)
    result = analyze(history, symbol="000001.SZ", market_dates=calendar(history))
    json.dumps(result, allow_nan=False)
    pd.testing.assert_frame_equal(history, original, check_exact=True)
    assert result["data_end"] == calendar(history)[-1]
    assert set(result["frequencies"]) == {"日线", "周线", "月线"}
    assert result["qualification"]["position_or_order_generated"] is False
    assert result["price_volume"]["formal_qualified"] is False
    assert result["source_evidence"]["status"] == "available"
    for period in result["frequencies"].values():
        for kind in ("fractals", "pens", "zones"):
            for item in period[kind]:
                if item["is_confirmed"]:
                    assert item["confirmed_at"] is not None
                    assert item["confirmed_at"] <= result["observed_at"]


def test_as_of_discards_future_prices_and_source_metadata(history):
    cut = 180
    day = calendar(history)[cut - 1]
    expected = analyze(history.iloc[:cut], as_of=day, market_dates=calendar(history))
    altered = history.copy(deep=True)
    altered.loc[cut:, ["open", "high", "low", "close"]] *= 12
    altered.loc[cut:, ["volume", "amount"]] *= 4
    altered.loc[cut:, "source"] = "future_source_not_verified"
    actual = analyze(altered, as_of=day, market_dates=calendar(history))
    assert actual == expected


def test_intraday_cutoff_excludes_unclosed_daily_bar(history):
    dates = calendar(history)
    result = analyze(history, as_of=dates[-1] + "T14:59:59+08:00", market_dates=dates)
    assert result["data_end"] == dates[-2]
    assert result["calendar"]["next_market_session"] == dates[-1]


def test_no_explicit_calendar_cannot_reuse_frame_attrs_for_qualification(history):
    supplied = history.copy()
    supplied.attrs["verified_market_dates"] = calendar(history)
    result = analyze(supplied)
    assert result["calendar"]["provided"] is False
    assert result["source_evidence"]["status"] == "unavailable"
    assert result["price_volume"]["input_eligible"] is False
    assert result["source_evidence"]["latest"]["structure"]["input_eligible"] is False


def test_unverified_csv_keeps_geometry_but_has_no_eligible_source_bs(history):
    supplied = history.copy()
    supplied["source"] = "csv_unverified"
    supplied["has_trade_price"] = 0
    supplied.attrs["price_basis"] = "unverified"
    result = analyze(supplied, market_dates=calendar(history))
    assert result["frequencies"]["日线"]["pens"]
    assert result["source_evidence"]["status"] == "unavailable"
    assert result["price_volume"]["input_eligible"] is False
    assert not any(item["candidate_eligible"] for item in result["events"])


@pytest.mark.parametrize("dates", [["2020-01-03", "2020-01-02"], ["2020-01-02", "2020-01-02"], []])
def test_calendar_is_never_sorted_or_deduplicated_silently(history, dates):
    with pytest.raises(ValueError, match="交易日历"):
        analyze(history, market_dates=dates)


def test_symbol_mismatch_and_beijing_requests_fail(history):
    with pytest.raises(ValueError):
        analyze(history, symbol="600519.SH", market_dates=calendar(history))
    with pytest.raises(ValueError):
        analyze(history.assign(symbol="430001.BJ"), market_dates=calendar(history))
