"""Small, standalone input and serialization contracts for frozen source rules."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd


# Preserve the frozen raw-source allowlist. An imported file's declaration is
# provenance supplied by its user; KCPredict does not independently certify it.
VERIFIED_SOURCES = frozenset({"tushare", "tushare:daily", "fuyao_unadjusted_v1",
    "baostock", "eastmoney_unadjusted_v1", "sina_unadjusted_v1",
    "tencent_star_unadjusted_v1"})


def stable_hash(data: Any) -> str:
    """Preserve the original evidence identity's canonical JSON convention."""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_value(value: Any) -> Any:
    """Make nested native/Pandas output strict JSON, with missing values null."""
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, np.ndarray):
        return [json_value(item) for item in value.tolist()]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, Path, date, datetime)):
        return str(value)
    return value


def normalize_symbol(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if re.fullmatch(r"\d{6}", value):
        if value.startswith("6"):
            value += ".SH"
        elif value.startswith(("0", "2", "3")):
            value += ".SZ"
        else:
            raise ValueError("请显式提供沪深股票后缀；北交所不在 KCPredict 范围内")
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", value):
        raise ValueError("股票代码必须为六位数字及 SH/SZ 后缀；北交所不受支持")
    return value


def validate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Original numerical geometry boundary; no sorting, filling or adjustment."""
    required = {"symbol", "date", "open", "high", "low", "close", "volume", "amount"}
    if frame.empty or not required <= set(frame.columns):
        raise ValueError("结构输入需要非空单股行情及完整 OHLCV/amount 字段")
    if frame["symbol"].isna().any() or frame["symbol"].astype(str).nunique() != 1:
        raise ValueError("结构输入不能混合不同股票行情")
    result = frame.copy()
    result["symbol"] = normalize_symbol(str(frame.iloc[0]["symbol"]))
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    if result["date"].isna().any() or result["date"].dt.tz is not None:
        raise ValueError("行情日期必须是有效本地日线日期")
    if (result["date"] != result["date"].dt.normalize()).any():
        raise ValueError("行情日期不能含盘中时刻")
    if result["date"].duplicated().any() or not result["date"].is_monotonic_increasing:
        raise ValueError("行情日期重复或乱序")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        result[column] = pd.to_numeric(result[column], errors="raise")
    prices = result[["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy(dtype=float)).all() or (prices <= 0).any().any():
        raise ValueError("OHLC 价格缺失或不合法；不能跳过日期推进结构")
    if (result["high"] < result[["open", "close", "low"]].max(axis=1)).any() or (
            result["low"] > result[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("OHLC 范围不合法")
    # Native geometry cannot consume non-finite quantity. Quantity-quality
    # failures remain explicit in source evidence; never manufacture eligibility.
    result["id"] = np.arange(len(result), dtype=int)
    result["dt"] = result["date"] + pd.Timedelta(hours=15)
    result.attrs = dict(frame.attrs)
    return result
