"""Standalone causal CZSC structures and original native price/volume evidence.

Confirmed geometry and source rules are extracted from the frozen local source.
The independent adapter accepts a verified OHLCV frame and explicit calendar;
it performs no project discovery, business database access or order execution.
"""
from __future__ import annotations

from typing import Any
import copy
import re

import pandas as pd

from .engine import engine_contract, native_runtime
from ._structure_contract import json_value, stable_hash, validate_frame
from ._structure_contract import normalize_symbol
from .indicators import chart_indicators, daily_volume_profile

FREQUENCIES = ("日线", "周线", "月线")
DEFAULT_CONFIG = {"frequencies": list(FREQUENCIES), "warmup_bars": 60,
                  "min_bi_len": 6, "max_bi_num": 200,
                  "higher_period_closure": "next_period_observed"}


def strategy_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze the same geometry defaults; environment variables cannot alter them."""
    result = {**copy.deepcopy(DEFAULT_CONFIG), **(config or {})}
    if result.get("higher_period_closure") != "next_period_observed":
        raise ValueError("高周期必须在下一周期被实际观测后闭合")
    if tuple(result.get("frequencies", [])) != FREQUENCIES:
        raise ValueError("只支持日线、周线、月线")
    for key, minimum in (("warmup_bars", 60), ("min_bi_len", 3), ("max_bi_num", 3)):
        value = result[key]
        if isinstance(value, bool) or int(value) != value or int(value) < minimum:
            raise ValueError(f"{key} 必须是至少 {minimum} 的整数")
        result[key] = int(value)
    contract = engine_contract()
    result.update(strategy_version="confirmed_structure", engine_contract=contract,
                  czsc_version=contract["build_version"], source_sha256=contract["source_tree_sha256"])
    return result


def to_raw_bar(row: Any):
    czsc = native_runtime()
    return czsc.RawBar(symbol=str(row.symbol), dt=row.dt, freq=czsc.Freq.D, id=int(row.id),
                       open=float(row.open), high=float(row.high), low=float(row.low),
                       close=float(row.close), vol=float(row.volume), amount=float(row.amount))


def _day(value: Any) -> str:
    return pd.Timestamp(value).date().isoformat()


def _pen_key(pen: Any) -> tuple[Any, ...]:
    # Native FX getters clone their payload. Keep both endpoints local to this
    # observation; an unfinished pen must never be cached across updates.
    first, last = pen.fx_a, pen.fx_b
    return (_day(first.dt), _day(last.dt), float(first.fx), float(last.fx))


def _zone_key(zone: Any) -> tuple[Any, ...]:
    return (_day(zone.sdt), _day(zone.edt), float(zone.zd), float(zone.zg))


def _forming_pen(analysis: Any, observed_at: str, pending_time: Any = None) -> dict[str, Any] | None:
    """Read a native unfinished stroke; its endpoint is still free to move."""
    unfinished = analysis.ubi
    if not unfinished:
        return None
    anchor = unfinished["fx_a"]
    # Native UBI includes the preceding neighbour of its starting fractal.
    # That neighbour cannot be the endpoint of a stroke starting later.
    raw_bars = [bar for bar in unfinished["raw_bars"] if bar.dt >= anchor.dt]
    if not raw_bars:
        return None
    downward = str(unfinished["direction"]) == "向下"
    endpoint = min(raw_bars, key=lambda bar: (float(bar.low), -pd.Timestamp(bar.dt).value)) if downward else max(
        raw_bars, key=lambda bar: (float(bar.high), pd.Timestamp(bar.dt).value))
    def visible_day(value: Any) -> str:
        return observed_at[:10] if pending_time is not None and value == pending_time else _day(value)
    start, end = visible_day(anchor.dt), visible_day(endpoint.dt)
    if start >= end:
        return None
    return {"start_time": start, "end_time": end, "start_price": float(anchor.fx),
            "end_price": float(endpoint.low if downward else endpoint.high),
            "confirmed_at": None, "is_confirmed": False, "observed_at": observed_at}


class IntentSession:
    """Read-only native structure context, without account or order state."""
    def __init__(self, symbol: str, config: dict[str, Any], *, create_position: bool = False):
        if create_position:
            raise ValueError("KCPredict structure analysis does not create a Position")
        self.czsc = native_runtime()
        self.config = config
        self.symbol = symbol
        self.position = None
        self.signals: dict[str, list[dict[str, Any]]] = {frequency: [] for frequency in FREQUENCIES}
        self.events: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []


class CzscSession(IntentSession):
    """Higher periods are published only after their next bucket is observed."""
    def __init__(self, symbol: str, config: dict[str, Any], count: int, *,
                 collect_confirmations: bool = True, create_position: bool = False):
        super().__init__(symbol, config, create_position=create_position)
        self.collect_confirmations = collect_confirmations
        # This generator only aggregates the current frequency buckets. Full
        # closed history and causal CZSC state live in closed/kas below. Two
        # retained bars preserve native last-bar aggregation and monotonically
        # increasing ids without exporting the entire history on every update.
        self.bg = self.czsc.BarGenerator(base_freq="日线", freqs=list(FREQUENCIES), max_count=2, market="A股")
        self.kas: dict[str, Any] = {}
        self.closed: dict[str, list[Any]] = {frequency: [] for frequency in FREQUENCIES}
        self.closed_available: dict[str, list[str]] = {frequency: [] for frequency in FREQUENCIES}
        self.divergences: dict[str, list[dict[str, Any]]] = {frequency: [] for frequency in FREQUENCIES}
        self.divergence_seen: dict[str, set[tuple]] = {frequency: set() for frequency in FREQUENCIES}
        self.pending: dict[str, Any] = {}
        self.confirmations: dict[str, dict[str, dict[tuple, str]]] = {
            frequency: {"fractals": {}, "pens": {}, "zones": {}} for frequency in FREQUENCIES}

    def _advance(self, frequency: str, bar: Any, available: str) -> None:
        self.closed[frequency].append(bar)
        if frequency not in self.kas:
            self.kas[frequency] = self.czsc.CZSC([bar], min_bi_len=int(self.config["min_bi_len"]), max_bi_num=int(self.config["max_bi_num"]))
        else:
            self.kas[frequency].update(bar)
        if not self.collect_confirmations:
            return
        self.closed_available[frequency].append(available)
        analysis = self.kas[frequency]
        confirmed = self.confirmations[frequency]
        # Confirm a fractal when the third inclusion-adjusted bar is present.
        for fractal in analysis.fx_list:
            confirmed["fractals"].setdefault((_day(fractal.dt), float(fractal.fx), str(fractal.mark)), available)
        for pen in analysis.finished_bis:
            confirmed["pens"].setdefault(_pen_key(pen), available)
        for zone in analysis.zs_list:
            if len(zone.bis) >= 3 and zone.is_valid():
                confirmed["zones"].setdefault(_zone_key(zone), available)
        finished = analysis.finished_bis
        if finished:
            last = finished[-1]
            key = _pen_key(last)
            if key not in self.divergence_seen[frequency]:
                self.divergence_seen[frequency].add(key)
                di = len(analysis.bi_list) - len(finished) + 1
                values = self.czsc._native.call_signal("cxt_five_bi_V230619", analysis, {"di": di})
                label = str(values[0].v1)
                if "底背驰" in label or "顶背驰" in label:
                    self.divergences[frequency].append({"time": key[1], "price": key[3],
                        "kind": "bottom" if "底背驰" in label else "top", "label": label,
                        "available_at": available, "confirmed_at": confirmed["pens"][key],
                        "is_confirmed": True, "signal_name": "cxt_five_bi_V230619", "di": di,
                        "pen_anchors": [list(_pen_key(pen)) for pen in finished[-5:]],
                        "scope": "原生五笔形态参考，非买卖确认；不改变策略"})

    def advance_structure(self, row: Any) -> tuple[str, float | None, float | None]:
        bar = to_raw_bar(row)
        date = _day(row.date)
        available = date + "T15:00:00+08:00"
        self.bg.update(bar)
        self._advance("日线", bar, available)
        higher_bars = self.bg.bars
        for frequency in ("周线", "月线"):
            latest = higher_bars[frequency][-1]
            previous = self.pending.get(frequency)
            if previous is not None and previous.dt != latest.dt:
                self._advance(frequency, previous, available)
            self.pending[frequency] = latest
        daily = self.kas["日线"].finished_bis
        direction = str(daily[-1].direction) if daily else "其他"
        weekly = self.closed["周线"]
        return direction, float(weekly[-1].close) if len(weekly) >= 2 else None, float(weekly[-2].close) if len(weekly) >= 2 else None

    def update(self, row: Any) -> None:
        """Advance causal geometry only; KCPredict has no position or order engine."""
        self.advance_structure(row)

    def payload(self, start: str | None = None) -> dict[str, Any]:
        if not self.collect_confirmations:
            raise RuntimeError("轻量决策会话没有结构确认时间，不能用于图表")
        result: dict[str, Any] = {}
        daily_bars = [{"time": _day(bar.dt), "high": float(bar.high), "low": float(bar.low),
                       "close": float(bar.close), "volume": float(bar.vol)} for bar in self.closed["日线"]]
        profile = daily_volume_profile(daily_bars)
        for frequency in FREQUENCIES:
            analysis = self.kas.get(frequency)
            bars = [{"time": _day(bar.dt), "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
                     "close": float(bar.close), "volume": float(bar.vol), "is_closed": True} for bar in self.closed[frequency]]
            pending = self.pending.get(frequency)
            availability = list(self.closed_available[frequency])
            observed_at = _day(self.closed["日线"][-1].dt) + "T15:00:00+08:00"
            display_analysis = analysis
            pending_time = None
            if pending is not None and self.closed["日线"] and not any(bar.dt == pending.dt for bar in self.closed[frequency]):
                observed_day = _day(self.closed["日线"][-1].dt)
                # Display the observed OHLC snapshot only; pending is never advanced
                # into the higher-frequency structure or Position before closure.
                bars.append({"time": observed_day, "bucket_time": _day(pending.dt),
                             "open": float(pending.open), "high": float(pending.high), "low": float(pending.low),
                             "close": float(pending.close), "volume": float(pending.vol), "is_closed": False,
                             "observed_at": observed_day + "T15:00:00+08:00"})
                availability.append(observed_day + "T15:00:00+08:00")
                # This independent display snapshot may supply forming strokes,
                # but never confirmations, signals, or the live session state.
                display_analysis = self.czsc.CZSC([*self.closed[frequency], pending],
                    min_bi_len=int(self.config["min_bi_len"]), max_bi_num=int(self.config["max_bi_num"]))
                pending_time = pending.dt
            fractals, pens, zones = [], [], []
            if analysis is not None:
                confirmed = self.confirmations[frequency]
                for fractal in analysis.fx_list:
                    key = (_day(fractal.dt), float(fractal.fx), str(fractal.mark))
                    fractals.append({"time": key[0], "price": key[1], "kind": "top" if str(fractal.mark) == "顶分型" else "bottom",
                                     "confirmed_at": confirmed["fractals"].get(key), "is_confirmed": key in confirmed["fractals"]})
                finished = {_pen_key(pen) for pen in analysis.finished_bis}
                for pen in analysis.bi_list:
                    key = _pen_key(pen)
                    if display_analysis is not analysis and key not in finished:
                        continue
                    pens.append({"start_time": key[0], "end_time": key[1], "start_price": key[2], "end_price": key[3],
                                 "confirmed_at": confirmed["pens"].get(key) if key in finished else None, "is_confirmed": key in finished})
                if display_analysis is not analysis:
                    last_confirmed_end = pens[-1]["end_time"] if pens else None
                    for pen in display_analysis.bi_list:
                        key = _pen_key(pen)
                        start_time = observed_at[:10] if pen.fx_a.dt == pending_time else key[0]
                        end_time = observed_at[:10] if pen.fx_b.dt == pending_time else key[1]
                        if key in finished or start_time >= end_time or (last_confirmed_end and start_time < last_confirmed_end):
                            continue
                        pens.append({"start_time": start_time, "end_time": end_time, "start_price": key[2], "end_price": key[3],
                                     "confirmed_at": None, "is_confirmed": False, "observed_at": observed_at})
                forming = _forming_pen(display_analysis, observed_at, pending_time)
                if forming and (not pens or forming["start_time"] >= pens[-1]["end_time"]):
                    pens.append(forming)
                for zone in analysis.zs_list:
                    if len(zone.bis) < 3 or not zone.is_valid():
                        continue
                    key = _zone_key(zone)
                    zones.append({"start_time": key[0], "end_time": key[1], "low": key[2], "high": key[3],
                                  "confirmed_at": confirmed["zones"].get(key), "is_confirmed": True})
            zones.sort(key=lambda item: (item["start_time"], item["end_time"]))
            previous = None
            for zone in zones:
                zone["direction"] = "up" if previous and zone["low"] > previous["low"] and zone["high"] > previous["high"] else "down" if previous and zone["low"] < previous["low"] and zone["high"] < previous["high"] else "range"
                previous = zone
            indicators = chart_indicators(bars, availability, profile, start, daily_bars=daily_bars)
            if start:
                bars = [item for item in bars if item["time"] >= start]
                fractals = [item for item in fractals if item["time"] >= start]
                pens = [item for item in pens if item["end_time"] >= start]
                zones = [item for item in zones if item["end_time"] >= start]
            result[frequency] = {"bars": bars, "fractals": fractals, "pens": pens, "zones": zones,
                                 "indicators": indicators,
                                 "divergences": [item for item in self.divergences[frequency] if not start or item["time"] >= start],
                                 "signals": [item for item in self.signals[frequency] if not start or item["time"] >= start]}
        return result


def _cutoff(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("无效 as_of")
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", str(value)):
        stamp += pd.Timedelta(hours=23, minutes=59, seconds=59)
    return stamp


def analyze(frame: pd.DataFrame, *, symbol: str | None = None,
            as_of: str | None = None, market_dates: list[str] | None = None,
            start: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze one supplied daily history without discovering project resources.

    ``market_dates`` must be an independently supplied, sorted, unique list of
    market-session dates. Missing calendars retain geometry but cannot qualify
    source B/S or price/volume inputs. Optional ``start`` only clips display;
    all history up to ``as_of`` supplies warmup. Date-only ``as_of`` includes
    that day's close; an earlier intraday timestamp excludes its daily bar.
    ``config`` may include ``czsc_strategy``, ``wyckoff`` and ``native_evidence``.
    No position targets, strategy orders or account state are generated.
    """
    from .vendor.native_evidence import build_native_evidence, prepare_native_wyckoff_inputs
    from .vendor.price_volume_features import W_INPUT_COLUMNS, get_wyckoff_profile

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame 必须是 pandas.DataFrame")
    original_attrs = dict(frame.attrs)
    work = frame.copy()
    if symbol is not None:
        requested = normalize_symbol(symbol)
        if "symbol" in work and any(normalize_symbol(item) != requested for item in work["symbol"]):
            raise ValueError("请求股票代码与输入行情不一致")
        work["symbol"] = requested
    if "date" not in work:
        raise ValueError("行情缺少 date 字段")
    if as_of is not None:
        cutoff = _cutoff(as_of)
        days = pd.to_datetime(work["date"], errors="raise")
        if days.dt.tz is not None:
            raise ValueError("行情日期必须是有效本地日线日期")
        work = work.loc[days + pd.Timedelta(hours=15) <= cutoff].copy()
    work.attrs = original_attrs
    work = validate_frame(work).reset_index(drop=True)
    work.attrs = original_attrs
    quantity = work[["volume", "amount"]]
    import numpy as np
    if not np.isfinite(quantity.to_numpy(dtype=float)).all() or (quantity < 0).any().any():
        raise ValueError("行情量额必须为非负有限数值")
    symbol = str(work.iloc[0].symbol)
    first_day, last_day = _day(work.iloc[0].date), _day(work.iloc[-1].date)
    lower = _day(start) if start is not None else None
    if lower and lower > last_day:
        raise ValueError("起始日期晚于可用行情截止日")
    if market_dates is not None:
        calendar = [_day(value) for value in market_dates]
        if not calendar or calendar != sorted(set(calendar)):
            raise ValueError("交易日历必须非空、排序且唯一")
        # Reject timestamps disguised as session dates instead of normalizing
        # duplicate or ambiguous calendar entries into a new provenance.
        if any(str(value) != day for value, day in zip(market_dates, calendar)):
            raise ValueError("交易日历条目必须使用 YYYY-MM-DD 格式")
    else:
        calendar = None
    # Calendar admission is explicit at this independent API boundary. An old
    # frame.attrs calendar must not silently overrule a missing argument.
    work.attrs.pop("verified_market_dates", None)
    if calendar is not None:
        work.attrs["verified_market_dates"] = list(calendar)
    policy = copy.deepcopy(config or {})
    settings = strategy_config(policy.get("czsc_strategy") or policy.get("strategy"))
    session = CzscSession(symbol, settings, len(work), create_position=False)
    for row in work.itertuples(index=False):
        session.advance_structure(row)
    prepared = prepare_native_wyckoff_inputs(work, config=policy, market_dates=calendar)
    evidence = build_native_evidence(work, config=policy, market_dates=calendar,
                                     prepared_wyckoff=prepared)
    latest = evidence["evaluations"][-1]
    w_display = prepared.frame.copy()
    if calendar is None:
        # Feature values remain inspectable, while admission follows the same
        # explicit-calendar requirement as the native evidence snapshots.
        w_display["wyckoff_input_eligible"] = False
        w_display["wyckoff_reason_codes"] = [list(codes) + ["native_verified_calendar_unavailable"]
                                               for codes in w_display.wyckoff_reason_codes]
    last_w = w_display.iloc[-1]
    w_columns = ["date", *W_INPUT_COLUMNS, "wyckoff_input_eligible", "wyckoff_reason_codes",
                 "wyckoff_event", "wyckoff_state", "wyckoff_anchor_at", "wyckoff_observed_at",
                 "wyckoff_available_at", "wyckoff_range_formed_at", "wyckoff_range_low",
                 "wyckoff_range_high", "wyckoff_stop", "wyckoff_rule_buy", "wyckoff_rule_sell",
                 "wyckoff_formal_qualified", "wyckoff_qualification_reasons"]
    w_frame = w_display.loc[:, [key for key in w_columns if key in w_display]].copy()
    w_frame["date"] = w_frame["date"].map(_day)
    if lower:
        w_frame = w_frame.loc[w_frame.date >= lower]
    calendar_set = set(calendar or [])
    unknown = [day for day in work.date.map(_day) if day not in calendar_set]
    next_session = next((day for day in (calendar or []) if day > last_day), None)
    source_eligible = bool(latest["structure"]["input_eligible"])
    w_eligible = bool(latest["wyckoff"]["input_eligible"])
    unverified = int((~pd.to_numeric(work["has_trade_price"], errors="coerce").eq(1)).sum()) if "has_trade_price" in work else len(work)
    profile = get_wyckoff_profile(policy.get("wyckoff"))
    events = [item for item in evidence["events"] if not lower or item["date"] >= lower]
    evaluations = [item for item in evidence["evaluations"] if not lower or item["date"] >= lower]
    warnings = [
        "周/月线在下一周期首次实际观测后才确认闭合；末桶与形成中笔可以变化。",
        "筹码图为过去120根日线的HLC/3成交量加权分布，不是股东真实持仓成本。",
        "来源 B/S、量价强度与背驰是研究参考，不代表成交、收益概率或收益认证。",
        "as_of 截断所提供的数据快照，未重建当时的数据修订版本。",
    ]
    if original_attrs.get("price_basis") == "unadjusted":
        warnings.append("使用未复权价格；缺少已核验公司行为、历史ST与退市时间线。")
    else:
        warnings.append("未复权价格口径尚未声明或核验；来源 B/S 和量价资格保持缺失。")
    if unverified:
        warnings.append(f"输入包含 {unverified} 根未声明为已核验真实交易价的日线。")
    if calendar is None:
        warnings.append("未提供独立交易日历；来源 B/S 和量价输入不能取得可用资格。")
    elif unknown:
        warnings.append(f"交易日历未覆盖 {len(unknown)} 个输入日期；受影响的依赖窗口不可用。")
    result = {
        "schema": "kcpredict_structure_v1", "symbol": symbol, "data_end": last_day,
        "as_of": as_of or last_day, "observed_at": last_day + "T15:00:00+08:00",
        "data_range": {"start": first_day, "end": last_day, "bars": len(work)},
        "data_version": stable_hash(json_value(work.to_dict("records"))),
        "price_basis": original_attrs.get("price_basis", "unverified"),
        "engine_contract": settings["engine_contract"],
        "strategy_version": settings["strategy_version"], "czsc_version": settings["czsc_version"],
        "source_sha256": settings["source_sha256"], "config_hash": stable_hash(settings),
        "frequencies": session.payload(lower), "events": events,
        "source_evidence": {
            "version": evidence["version"], "schema_hash": evidence["schema_hash"],
            "config_hash": evidence["config_hash"], "semantics": evidence["semantics"],
            "status": "available" if source_eligible and w_eligible else "partial" if source_eligible or w_eligible else "unavailable",
            "latest": latest, "events": events, "evaluations": evaluations,
            "auxiliary_evaluations": [item for item in evidence["auxiliary_evaluations"] if not lower or item["date"] >= lower],
            "calendar_provenance": evidence["calendar_provenance"],
        },
        "price_volume": {
            "profile": profile, "columns": list(W_INPUT_COLUMNS), "rows": w_frame.to_dict("records"),
            "latest": {key: last_w[key] for key in w_columns if key in last_w.index},
            "input_eligible": bool(last_w.wyckoff_input_eligible),
            "reason_codes": list(last_w.wyckoff_reason_codes), "formal_qualified": False,
        },
        "input_quality": {"unverified_trade_price_bars": unverified,
            "sources": sorted(work.source.fillna("").astype(str).unique().tolist()) if "source" in work else [],
            "volume_unit": original_attrs.get("volume_unit", "unverified"),
            "amount_unit": original_attrs.get("amount_unit", "unverified"),
            "source_declarations": "supplied_input_metadata_not_independent_provider_certification"},
        "calendar": {"provided": calendar is not None, "unknown_input_dates": unknown,
            "next_market_session": next_session, "prefix_hash": stable_hash([day for day in (calendar or []) if day <= last_day])},
        "warmup": {"required": settings["warmup_bars"], "available": len(work),
            "eligible": len(work) >= settings["warmup_bars"]},
        "qualification": {"scope": "research_reference", "formal_qualified": False,
            "model_used": False, "position_or_order_generated": False},
        "warnings": warnings,
    }
    return json_value(result)


analyze_structure = analyze

