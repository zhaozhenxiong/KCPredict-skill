"""Portable, transactional local input contracts; no workstation data is read."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from kcpredict.data import Store
from kcpredict.prediction import prepare_input


def bars(count=20):
    close = np.linspace(10.1, 12.7, count)
    return pd.DataFrame({"date": pd.bdate_range("2026-01-05", periods=count).strftime("%Y-%m-%d"),
        "symbol": "000001.SZ", "open": close - .1, "high": close + .8, "low": close - .7,
        "close": close, "volume": np.linspace(100000.1, 200000.9, count),
        "amount": close * 100000.123456789, "source": "sina_unadjusted_v1", "has_trade_price": 1})


def csv(tmp_path, frame, name="bars.csv"):
    path = tmp_path / name
    frame.to_csv(path, index=False, float_format="%.17g")
    return path


def test_independent_home_and_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    store = Store()
    assert store.path == tmp_path / "state/data.sqlite3"
    assert store.symbols() == []
    dates, identity = store.calendar()
    assert dates == [] and not identity["verified"]
    assert not identity["externally_verified"]
    with pytest.raises(ValueError, match="No local"):
        store.load_bars("000001")


def test_roundtrip_float_bytes_and_repeated_import_reuse(tmp_path):
    frame = bars()
    source = csv(tmp_path, frame)
    store = Store(tmp_path / "state")
    result = store.import_csv(source, verified_unadjusted=True)
    assert result["inserted"] == len(frame) and result["duplicates"] == 0
    assert result["provenance"] == "user_asserted" and not result["externally_verified"]
    actual = store.load_bars("000001")
    np.testing.assert_array_equal(actual[["open", "high", "low", "close", "volume", "amount"]].to_numpy(),
                                  frame[["open", "high", "low", "close", "volume", "amount"]].to_numpy())
    assert actual.attrs["price_basis"] == "unadjusted"
    assert actual.attrs["source_files"] == [hashlib.sha256(source.read_bytes()).hexdigest()]
    assert actual.attrs["imports"][0]["filename"] == "bars.csv"
    assert str(tmp_path) not in json.dumps(actual.attrs)
    second = store.import_csv(source, verified_unadjusted=True)
    assert second["inserted"] == 0 and second["duplicates"] == len(frame)
    pd.testing.assert_frame_equal(actual, store.load_bars("000001.SZ"))
    assert store.symbols() == ["000001.SZ"]


def test_default_import_does_not_certify_provider_claims(tmp_path):
    source = csv(tmp_path, bars())
    store = Store(tmp_path / "state")
    store.import_csv(source)
    frame = store.load_bars("000001")
    assert set(frame.source) == {"csv_unverified"}
    assert set(frame.declared_source) == {"sina_unadjusted_v1"}
    assert set(frame.has_trade_price) == {0}
    assert frame.attrs["price_basis"] == "unverified"
    assert frame.attrs["provenance"] == "user_asserted"


@pytest.mark.parametrize("mutation", [
    lambda frame: frame.drop(columns="source"),
    lambda frame: frame.assign(source="unspecified_csv_vendor"),
    lambda frame: frame.drop(columns="has_trade_price"),
])
def test_unverified_source_or_missing_trade_price_cannot_gain_contract(tmp_path, mutation):
    source = csv(tmp_path, mutation(bars()))
    store = Store(tmp_path / "state")
    store.import_csv(source, verified_unadjusted=True)
    frame = store.load_bars("000001")
    assert not ((frame.source == "sina_unadjusted_v1") & (frame.has_trade_price == 1)).any()


def test_bare_code_column_retains_leading_zeroes_and_explicit_symbol_is_checked(tmp_path):
    frame = bars(3).assign(symbol="000001")
    source = csv(tmp_path, frame)
    store = Store(tmp_path / "state")
    store.import_csv(source, symbol="000001.SZ")
    assert store.symbols() == ["000001.SZ"]
    with pytest.raises(ValueError, match="differs"):
        store.import_csv(source, symbol="000002.SZ")


def test_missing_symbol_requires_explicit_argument(tmp_path):
    source = csv(tmp_path, bars(3).drop(columns="symbol"))
    store = Store(tmp_path / "state")
    with pytest.raises(ValueError, match="symbol"):
        store.import_csv(source)
    assert store.import_csv(source, symbol="000001")["inserted"] == 3


@pytest.mark.parametrize("symbol", ["830001.BJ", "830001", "430001", "920001", "123", "bad.SZ"])
def test_beijing_and_invalid_symbols_are_rejected(tmp_path, symbol):
    source = csv(tmp_path, bars(3).assign(symbol=symbol))
    store = Store(tmp_path / "state")
    with pytest.raises(ValueError):
        store.import_csv(source)
    assert store.symbols() == []


def test_explicit_shanghai_b_share_is_not_misclassified(tmp_path):
    source = csv(tmp_path, bars(3).assign(symbol="900901.SH"))
    store = Store(tmp_path / "state")
    assert store.import_csv(source)["inserted"] == 3
    assert store.symbols() == ["900901.SH"]


@pytest.mark.parametrize("mutation", [
    lambda frame: frame.assign(date="2026-01-05T10:00:00"),
    lambda frame: frame.assign(date="2026-01-05T00:00:00+08:00"),
    lambda frame: frame.assign(date=None),
    lambda frame: pd.concat([frame, frame.iloc[[0]]]),
    lambda frame: frame.iloc[::-1],
    lambda frame: frame.assign(high=frame.low),
    lambda frame: frame.assign(close=0),
    lambda frame: frame.assign(volume=-1),
    lambda frame: frame.assign(amount=np.inf),
    lambda frame: frame.assign(has_trade_price=2),
    lambda frame: frame.drop(columns="amount"),
])
def test_invalid_inputs_are_rejected_before_rows_are_committed(tmp_path, mutation):
    source = csv(tmp_path, mutation(bars(3)))
    store = Store(tmp_path / "state")
    with pytest.raises((ValueError, TypeError)):
        store.import_csv(source)
    assert store.symbols() == []


def test_conflict_rolls_back_entire_batch_and_declaration_cannot_be_promoted(tmp_path):
    frame = bars(4)
    store = Store(tmp_path / "state")
    store.import_csv(csv(tmp_path, frame.iloc[:2]))
    before = store.load_bars("000001")
    changed = frame.copy()
    changed.loc[0, "amount"] += 1
    with pytest.raises(ValueError, match="Conflicting"):
        store.import_csv(csv(tmp_path, changed, "conflict.csv"))
    pd.testing.assert_frame_equal(store.load_bars("000001"), before)
    with pytest.raises(ValueError, match="Conflicting"):
        store.import_csv(csv(tmp_path, frame.iloc[:2]), verified_unadjusted=True)
    assert store.load_bars("000001").attrs["price_basis"] == "unverified"


def test_overlapping_identical_rows_are_reused_and_new_days_append(tmp_path):
    frame = bars(4)
    store = Store(tmp_path / "state")
    store.import_csv(csv(tmp_path, frame.iloc[:3]))
    appended = store.import_csv(csv(tmp_path, frame.iloc[2:], "append.csv"))
    assert appended["inserted"] == appended["duplicates"] == 1
    assert len(store.load_bars("000001")) == 4


def test_load_end_preserves_daily_close_availability(tmp_path):
    frame = bars(4)
    store = Store(tmp_path / "state")
    store.import_csv(csv(tmp_path, frame))
    day = frame.date.iloc[2]
    assert len(store.load_bars("000001", day)) == 3
    assert len(store.load_bars("000001", day + "T14:59:59+08:00")) == 2
    assert len(store.load_bars("000001", day + "T15:00:00+08:00")) == 3
    assert len(store.load_bars("000001", day + "T07:00:00+00:00")) == 3


def test_calendar_explicit_declaration_and_content_identity(tmp_path):
    days = bars(4)[["date"]]
    source = csv(tmp_path, days, "calendar.csv")
    store = Store(tmp_path / "state")
    result = store.import_calendar(source, verified=True)
    assert result["inserted"] == 4 and result["verified"]
    dates, identity = store.calendar()
    assert dates == days.date.tolist()
    assert identity["verified"] and identity["provenance"] == "user_asserted"
    assert not identity["externally_verified"]
    assert identity["source_sha256"] == [hashlib.sha256(source.read_bytes()).hexdigest()]
    assert store.import_calendar(source, verified=True)["duplicates"] == 4
    assert store.calendar() == (dates, identity)
    source2 = csv(tmp_path, pd.DataFrame({"date": ["2026-01-09"]}), "next.csv")
    store.import_calendar(source2, verified=True)
    assert store.calendar()[1]["dates_sha256"] != identity["dates_sha256"]


def test_unverified_calendar_is_explicit_and_conflicting_upgrade_is_rejected(tmp_path):
    source = csv(tmp_path, bars(4)[["date"]], "calendar.csv")
    store = Store(tmp_path / "state")
    store.import_calendar(source)
    before = store.calendar()
    assert not before[1]["verified"]
    with pytest.raises(ValueError, match="Conflicting"):
        store.import_calendar(source, verified=True)
    assert store.calendar() == before


@pytest.mark.parametrize("dates", [
    ["2026-01-05", "2026-01-05"], ["2026-01-06", "2026-01-05"],
    ["2026-01-05T15:00:00"], ["2026-01-05T00:00:00+08:00"], [None],
])
def test_bad_calendar_never_imports_partial_dates(tmp_path, dates):
    source = csv(tmp_path, pd.DataFrame({"date": dates}), "calendar.csv")
    store = Store(tmp_path / "state")
    with pytest.raises(ValueError):
        store.import_calendar(source, verified=True)
    assert store.calendar()[0] == []


def test_portable_stored_frame_preserves_prediction_input_contract(tmp_path):
    frame = bars()
    store = Store(tmp_path / "state")
    store.import_csv(csv(tmp_path, frame), verified_unadjusted=True)
    day = frame.date.iloc[-1]
    loaded = store.load_bars("000001")
    _input, _future, contract = prepare_input(loaded, ["2026-02-02"], symbol="000001.SZ",
        as_of=day, lookback=16, horizon=1)
    assert contract["source"] == ["sina_unadjusted_v1"]
    assert contract["unverified_trade_price_bars"] == 0
    assert contract["price_basis"] == "unadjusted"
    assert contract["volume_unit"] == "shares" and contract["amount_unit"] == "CNY"


def test_database_reopens_without_source_csv_and_retains_calendar(tmp_path):
    home = tmp_path / "state"
    source = csv(tmp_path, bars())
    calendar = csv(tmp_path, bars()[["date"]], "calendar.csv")
    store = Store(home)
    store.import_csv(source)
    store.import_calendar(calendar, verified=True)
    expected_bars, expected_calendar = store.load_bars("000001"), store.calendar()
    source.unlink()
    calendar.unlink()
    reopened = Store(home)
    pd.testing.assert_frame_equal(reopened.load_bars("000001"), expected_bars)
    assert reopened.calendar() == expected_calendar
