import unittest
import contextlib
import io
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from skill.scripts.ict_scan import (
    Candle, _candidate_target, _latest_swing, detect_fvgs, detect_swings, main, parse_candles, scan,
)


def bullish_fixture():
    base = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
    values = [
        (101, 103, 99, 102),
        (102, 115, 100, 110),
        (110, 111, 98, 100),
        (100, 103, 99, 102),
        (102, 105, 100, 104),
        (104, 104.5, 99, 100),
        (100, 101, 97, 99),
        (99, 104, 98.8, 103.8),
        (103.8, 110, 103, 106),
        (106, 107, 101.5, 103),
    ]
    return {
        "meta": {"instrument": "TEST-USDT", "timeframe": "5m"},
        "candles": [
            {
                "timestamp": (base + timedelta(minutes=index * 5)).isoformat(),
                "open": row[0],
                "high": row[1],
                "low": row[2],
                "close": row[3],
                "volume": 100 + index,
                "is_closed": True,
            }
            for index, row in enumerate(values)
        ],
    }


class FvgTests(unittest.TestCase):
    def test_strict_wick_non_overlap_is_required(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [
            Candle(base, 9, 10, 8, 9),
            Candle(base + timedelta(minutes=5), 9, 12, 8.5, 11),
            Candle(base + timedelta(minutes=10), 11, 12, 9.9, 11.5),
        ]
        self.assertEqual(detect_fvgs(candles), [])

    def test_bullish_fvg_zone_and_ce(self):
        candles, _ = parse_candles(bullish_fixture())
        gap = next(gap for gap in detect_fvgs(candles) if gap["middle_index"] == 7)
        self.assertEqual(gap["direction"], "bullish")
        self.assertEqual(gap["zone"], [101.0, 103.0])
        self.assertEqual(gap["ce"], 102.0)


class IctModelTests(unittest.TestCase):
    def run_fixture(self, payload=None):
        candles, meta = parse_candles(payload or bullish_fixture())
        return scan(
            candles,
            meta,
            swing_width=1,
            setup_lookahead=5,
            displacement_body_multiple=1.2,
            displacement_atr_multiple=0.5,
            minimum_rr=2.0,
            direction="long",
        )

    def test_complete_bullish_ict_sequence_confirms(self):
        result = self.run_fixture()
        candidate = result["latest_candidate"]
        self.assertEqual(candidate["mechanical_status"], "confirmed")
        self.assertEqual(candidate["sweep"]["session"], "silver_bullet")
        self.assertEqual(candidate["entry"]["zone"], [101.0, 103.0])
        self.assertEqual(candidate["mss"]["reference"]["price"], 105.0)
        self.assertEqual(candidate["target"]["price"], 115.0)
        self.assertGreaterEqual(candidate["gross_rr_to_candidate_target"], 2.0)
        self.assertTrue(all(candidate["checks"].values()))

    def test_bearish_mirror_uses_the_same_gates_and_lifecycle(self):
        payload = bullish_fixture()
        for row in payload["candles"]:
            original = {field: row[field] for field in ("open", "high", "low", "close")}
            row.update(open=200 - original["open"], high=200 - original["low"],
                       low=200 - original["high"], close=200 - original["close"])
        candles, meta = parse_candles(payload)
        result = scan(candles, meta, swing_width=1, setup_lookahead=5,
                      displacement_body_multiple=1.2, displacement_atr_multiple=0.5,
                      minimum_rr=2, direction="short")
        candidate = self.candidate_at_sweep(result)
        self.assertEqual(candidate["mechanical_status"], "confirmed")
        self.assertEqual(candidate["entry"]["zone"], [97, 99])
        self.assertEqual(candidate["target"]["price"], 85)
        self.assertEqual(candidate["invalidation"]["structural_level"], 103)

    def test_wick_without_close_does_not_confirm_mss(self):
        payload = bullish_fixture()
        payload["candles"][8]["close"] = 104
        result = self.run_fixture(payload)
        candidate = result["latest_candidate"]
        self.assertEqual(candidate["mechanical_status"], "developing")
        self.assertFalse(candidate["checks"]["closed_candle_mss"])

    def test_okx_newest_first_arrays_are_sorted(self):
        payload = bullish_fixture()
        arrays = [
            [
                int(datetime.fromisoformat(row["timestamp"]).timestamp() * 1000),
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ]
            for row in reversed(payload["candles"])
        ]
        candles, _ = parse_candles(arrays)
        self.assertLess(candles[0].timestamp, candles[-1].timestamp)

    def candidate_at_sweep(self, result, index=6):
        return next(candidate for candidate in result["candidates"] if candidate["sweep"]["index"] == index)

    def append_candle(self, payload, values, *, is_closed=True):
        timestamp = datetime.fromisoformat(payload["candles"][-1]["timestamp"]) + timedelta(minutes=5)
        payload["candles"].append(dict(zip(("open", "high", "low", "close"), values),
                                        timestamp=timestamp.isoformat(), is_closed=is_closed))

    def test_fvg_edge_touch_does_not_fill_ce_entry(self):
        payload = bullish_fixture()
        payload["candles"][9]["low"] = 102.5
        candidate = self.candidate_at_sweep(self.run_fixture(payload))
        self.assertEqual(candidate["mechanical_status"], "developing")
        self.assertIsNone(candidate["entry"]["retest_index"])

    def test_close_through_fvg_does_not_confirm_retest(self):
        payload = bullish_fixture()
        payload["candles"][9].update(low=100, close=100.5)
        candidate = self.candidate_at_sweep(self.run_fixture(payload))
        self.assertEqual(candidate["mechanical_status"], "no-trade")
        self.assertEqual(candidate["lifecycle"]["fvg_failure_index"], 9)

    def test_later_stop_invalidation_is_not_hidden_by_a_past_retest(self):
        payload = bullish_fixture()
        self.append_candle(payload, (103, 104, 96, 98))
        candidate = self.candidate_at_sweep(self.run_fixture(payload))
        self.assertEqual(candidate["entry"]["retest_index"], 9)
        self.assertEqual(candidate["mechanical_status"], "invalidated")
        self.assertEqual(candidate["invalidation"]["crossed_index"], 10)

    def test_target_after_retest_completes_the_opportunity(self):
        payload = bullish_fixture()
        self.append_candle(payload, (103, 116, 102, 114))
        candidate = self.candidate_at_sweep(self.run_fixture(payload))
        self.assertEqual(candidate["mechanical_status"], "no-trade")
        self.assertEqual(candidate["lifecycle"]["target_reached_index"], 10)
        self.assertIn("complete", candidate["reason"])

    def test_same_bar_entry_target_or_stop_order_is_unknown(self):
        for high, low in ((116, 101.5), (107, 96), (116, 96)):
            with self.subTest(high=high, low=low):
                payload = bullish_fixture()
                payload["candles"][9].update(high=high, low=low)
                candidate = self.candidate_at_sweep(self.run_fixture(payload))
                self.assertEqual(candidate["mechanical_status"], "no-trade")
                self.assertEqual(candidate["lifecycle"]["ambiguous_index"], 9)
                self.assertIsNone(candidate["entry"]["retest_index"])

    def test_same_bar_target_and_stop_after_retest_remain_ambiguous(self):
        payload = bullish_fixture()
        self.append_candle(payload, (103, 116, 96, 104))
        candidate = self.candidate_at_sweep(self.run_fixture(payload))
        self.assertEqual(candidate["mechanical_status"], "no-trade")
        self.assertEqual(candidate["lifecycle"]["ambiguous_index"], 10)

    def test_structure_can_fail_before_fvg_confirmation_or_without_fvg(self):
        for third_low in (103, 100):
            with self.subTest(third_low=third_low):
                payload = bullish_fixture()
                payload["candles"][7]["low"] = 96
                payload["candles"][8]["low"] = third_low
                candidate = self.candidate_at_sweep(self.run_fixture(payload))
                self.assertEqual(candidate["mechanical_status"], "invalidated")
                self.assertEqual(candidate["invalidation"]["crossed_index"], 7)

    def test_explicit_open_tail_is_excluded_from_all_detections(self):
        payload = bullish_fixture()
        baseline = self.run_fixture(payload)
        self.append_candle(payload, (103, 104, 96, 98), is_closed=False)
        result = self.run_fixture(payload)
        self.assertEqual(result["counts"], baseline["counts"])
        self.assertEqual(result["latest_candidate"], baseline["latest_candidate"])
        self.assertEqual(result["meta"]["candle_count"], 10)
        self.assertEqual(result["meta"]["excluded_open_candle_count"], 1)

    def test_okx_zero_confirm_tail_is_excluded(self):
        payload = bullish_fixture()
        self.append_candle(payload, (103, 104, 96, 98), is_closed=False)
        arrays = [[row["timestamp"], row["open"], row["high"], row["low"], row["close"],
                   row.get("volume", 0), 0, 0, "1" if row["is_closed"] else "0"]
                  for row in payload["candles"]]
        result = self.run_fixture(arrays)
        self.assertEqual(result["meta"]["excluded_open_candle_count"], 1)
        self.assertEqual(result["latest_candidate"]["mechanical_status"], "confirmed")

    def test_unknown_closure_is_an_explicit_user_declaration_warning(self):
        payload = bullish_fixture()
        for row in payload["candles"]:
            row.pop("is_closed")
        result = self.run_fixture(payload)
        self.assertEqual(result["meta"]["closure_unverified_count"], 10)
        self.assertTrue(any("closure is unverified" in warning for warning in result["warnings"]))

    def test_open_candle_cannot_make_ten_closed_candles(self):
        payload = bullish_fixture()
        payload["candles"][-1]["is_closed"] = False
        with self.assertRaisesRegex(ValueError, "10 closed"):
            self.run_fixture(payload)

    def test_open_candle_inside_closed_history_is_rejected(self):
        payload = bullish_fixture()
        payload["candles"][3]["is_closed"] = False
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            self.run_fixture(payload)


class CausalityTests(unittest.TestCase):
    def test_swing_records_when_its_right_hand_confirmation_is_available(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        highs = [10, 11, 15, 12, 11, 10]
        candles = [Candle(base + timedelta(days=index), high - 2, high, high - 3, high - 1)
                   for index, high in enumerate(highs)]
        swings = detect_swings(candles, 2)
        high = next(swing for swing in swings if swing["kind"] == "high")
        self.assertEqual(high["confirmed_index"], 4)
        self.assertEqual(high["confirmed_time"], "2026-01-05T00:00:00Z")
        # An anchor seen in the complete chart is unavailable at the old cutoff.
        self.assertIsNone(_latest_swing(swings, "high", 3))
        self.assertIsNone(_candidate_target(swings, "long", 3, 12))
        self.assertEqual(_latest_swing(swings, "high", 5), high)
        self.assertEqual(_candidate_target(swings, "long", 5, 12), high)

    def test_already_consumed_target_is_not_reused(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [Candle(base + timedelta(days=index), 9, high, 8, 9)
                   for index, high in enumerate([10, 15, 11, 16, 10])]
        target = {"kind": "high", "index": 1, "price": 15, "confirmed_index": 2}
        self.assertIsNone(_candidate_target([target], "long", 4, 12, candles))


class InputAndCliTests(unittest.TestCase):
    def test_nonfinite_prices_volume_and_timestamps_are_rejected(self):
        for field in ("open", "high", "low", "close", "volume", "timestamp"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    payload = bullish_fixture()
                    payload["candles"][0][field] = value
                    with self.assertRaisesRegex(ValueError, "finite"):
                        parse_candles(payload)

    def test_invalid_closure_marker_is_rejected(self):
        payload = bullish_fixture()
        payload["candles"][0]["is_closed"] = "maybe"
        with self.assertRaisesRegex(ValueError, "closure flag"):
            parse_candles(payload)

    def test_parameters_must_be_finite_and_positive(self):
        candles, meta = parse_candles(bullish_fixture())
        for parameter in ("swing_width", "sweep_lookback", "setup_lookahead", "atr_period",
                          "displacement_body_multiple", "displacement_atr_multiple", "minimum_rr"):
            for value in (0, -1, float("nan"), True):
                with self.subTest(parameter=parameter, value=value):
                    with self.assertRaises(ValueError):
                        scan(candles, meta, **{parameter: value})

    def test_output_refuses_overwrite_and_returns_controlled_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candles.json"
            input_path.write_text(json.dumps(bullish_fixture()), encoding="utf-8")
            output_path = Path(directory) / "existing.json"
            output_path.write_text("keep me", encoding="utf-8")
            for destination in (output_path, Path(directory) / "missing" / "result.json"):
                with self.subTest(destination=destination):
                    error = io.StringIO()
                    with patch("sys.argv", ["ict_scan.py", "--input", str(input_path), "--output", str(destination)]), \
                            contextlib.redirect_stderr(error):
                        self.assertEqual(main(), 2)
                    self.assertIn("error:", error.getvalue())
            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
