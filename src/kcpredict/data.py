"""Independent append-only CSV inputs and explicit local provenance.

KCPredict does not certify CSV claims or obtain market data. Verified flags
record the importing user's declaration. Conflicting stored observations are
rejected together in one transaction, rather than silently revised.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from ._structure_contract import normalize_symbol, VERIFIED_SOURCES

FEATURES = ("open", "high", "low", "close", "volume", "amount")
_BAR_FIELDS = ("symbol", "date", *FEATURES, "source", "declared_source",
               "has_trade_price", "price_basis", "volume_unit", "amount_unit", "name", "provenance")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _day(value: Any) -> str:
    """Require actual midnight dates; never coerce an intraday bar to a day."""
    if value is None or pd.isna(value):
        raise ValueError("CSV dates must be nonempty local daily dates")
    try:
        # A date written as an integer is YYYYMMDD, not epoch nanoseconds.
        stamp = pd.Timestamp(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("CSV dates must be valid local daily dates") from exc
    if pd.isna(stamp) or stamp.tzinfo is not None or stamp != stamp.normalize():
        raise ValueError("CSV dates must be timezone-free midnight daily dates")
    return stamp.date().isoformat()


def _read_csv(path: str | Path) -> tuple[pd.DataFrame, str, str]:
    location = Path(path).expanduser().resolve()
    raw = location.read_bytes()
    try:
        frame = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype={"symbol": "string"},
                            float_precision="round_trip")
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise ValueError("Expected a nonempty UTF-8 CSV with a header") from exc
    if frame.empty:
        raise ValueError("CSV must contain at least one row")
    return frame, hashlib.sha256(raw).hexdigest(), location.name


def _unique_contract(values) -> str:
    values = sorted(set(values))
    return values[0] if len(values) == 1 else "mixed"


class Store:
    """Own a standalone SQLite database below KCPREDICT_HOME or ~/.kcpredict."""

    def __init__(self, home: str | Path | None = None):
        self.home = Path(home or os.environ.get("KCPREDICT_HOME") or Path.home() / ".kcpredict").expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.db_path = self.home / "data.sqlite3"
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, date TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, volume REAL NOT NULL, amount REAL NOT NULL,
                    source TEXT NOT NULL, declared_source TEXT NOT NULL,
                    has_trade_price INTEGER NOT NULL, price_basis TEXT NOT NULL,
                    volume_unit TEXT NOT NULL, amount_unit TEXT NOT NULL,
                    name TEXT NOT NULL, provenance TEXT NOT NULL,
                    import_id TEXT NOT NULL REFERENCES imports(id),
                    PRIMARY KEY (symbol, date)
                );
                CREATE TABLE IF NOT EXISTS market_calendar (
                    date TEXT PRIMARY KEY, verified INTEGER NOT NULL,
                    source TEXT NOT NULL, provenance TEXT NOT NULL,
                    import_id TEXT NOT NULL REFERENCES imports(id)
                );
                PRAGMA user_version = 1;
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record_import(connection, import_id, kind, source_sha, metadata):
        connection.execute("INSERT OR IGNORE INTO imports VALUES (?, ?, ?, ?, ?)",
            (import_id, kind, source_sha, json.dumps(metadata, ensure_ascii=False, sort_keys=True),
             datetime.now(timezone.utc).isoformat()))

    def import_csv(self, path: str | Path, symbol: str | None = None, *,
                   verified_unadjusted: bool = False, volume_unit: str = "shares",
                   amount_unit: str = "CNY") -> dict:
        frame, source_sha, filename = _read_csv(path)
        if not {"date", *FEATURES} <= set(frame.columns):
            raise ValueError("Daily CSV requires date, open, high, low, close, volume and amount")
        if not isinstance(verified_unadjusted, bool):
            raise ValueError("verified_unadjusted must be an explicit boolean declaration")
        volume_unit = str(volume_unit).strip()
        amount_unit = str(amount_unit).strip()
        if not volume_unit or not amount_unit:
            raise ValueError("Volume and amount units must be explicitly described")
        requested = normalize_symbol(symbol) if symbol is not None else None
        if "symbol" not in frame:
            if requested is None:
                raise ValueError("Provide --symbol or a symbol column in the CSV")
            frame["symbol"] = requested
        if frame["symbol"].isna().any():
            raise ValueError("CSV symbols cannot be empty")
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
        if requested is not None and set(frame["symbol"]) != {requested}:
            raise ValueError("CSV symbol differs from the explicitly requested symbol")
        frame["date"] = frame["date"].map(_day)
        if frame.duplicated(["symbol", "date"]).any():
            raise ValueError("CSV has duplicate symbol/date observations")
        for _key, group in frame.groupby("symbol", sort=False):
            if not group["date"].is_monotonic_increasing:
                raise ValueError("Daily CSV dates must increase within each symbol")
        for field in FEATURES:
            frame[field] = pd.to_numeric(frame[field], errors="raise").astype(np.float64)
        values = frame[list(FEATURES)].to_numpy(dtype=np.float64)
        if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4:] < 0).any():
            raise ValueError("Daily OHLC must be finite and positive; volume/amount finite and nonnegative")
        if ((values[:, 1] < values[:, [0, 2, 3]].max(axis=1)) |
                (values[:, 2] > values[:, [0, 1, 3]].min(axis=1))).any():
            raise ValueError("Daily OHLC geometry is invalid")
        if "has_trade_price" in frame:
            trade_prices = pd.to_numeric(frame["has_trade_price"], errors="raise")
            if trade_prices.isna().any() or not trade_prices.isin((0, 1)).all():
                raise ValueError("has_trade_price must contain only 0 or 1")
        else:
            trade_prices = pd.Series(0, index=frame.index)
        declared = frame["source"].fillna("").astype(str).str.strip() if "source" in frame else pd.Series("", index=frame.index)
        admitted = bool(verified_unadjusted) & declared.isin(VERIFIED_SOURCES)
        frame["declared_source"] = declared
        frame["source"] = declared.where(admitted, "csv_unverified")
        frame["has_trade_price"] = trade_prices.where(admitted, 0).astype(int)
        frame["price_basis"] = "unadjusted" if verified_unadjusted else "unverified"
        frame["volume_unit"], frame["amount_unit"] = volume_unit, amount_unit
        frame["name"] = frame["name"].fillna("").astype(str) if "name" in frame else ""
        frame["provenance"] = "user_asserted"
        metadata = {"schema": "kcpredict_csv_import_v1", "filename": filename,
                    "source_sha256": source_sha, "provenance": "user_asserted",
                    "externally_verified": False, "verified_unadjusted": verified_unadjusted,
                    "volume_unit": volume_unit, "amount_unit": amount_unit,
                    "symbols": sorted(set(frame["symbol"])), "rows": len(frame)}
        import_id = _hash({**metadata, "filename": None})
        rows = list(frame[list(_BAR_FIELDS)].itertuples(index=False, name=None))
        inserted, duplicates = [], 0
        fields = ", ".join(_BAR_FIELDS)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                existing = connection.execute(f"SELECT {fields} FROM bars WHERE symbol = ? AND date = ?", row[:2]).fetchone()
                if existing is None:
                    inserted.append(row)
                elif tuple(existing) == row:
                    duplicates += 1
                else:
                    raise ValueError(f"Conflicting stored observation: {row[0]} {row[1]}; no rows imported")
            if inserted:
                self._record_import(connection, import_id, "bars", source_sha, metadata)
                placeholders = ", ".join("?" for _ in (*_BAR_FIELDS, "import_id"))
                connection.executemany(f"INSERT INTO bars ({fields}, import_id) VALUES ({placeholders})",
                                       [(*row, import_id) for row in inserted])
        return {**metadata, "status": "imported" if inserted else "reused", "import_id": import_id,
                "inserted": len(inserted), "duplicates": duplicates}

    def import_calendar(self, path: str | Path, *, verified: bool = False) -> dict:
        frame, source_sha, filename = _read_csv(path)
        if "date" not in frame:
            raise ValueError("Calendar CSV requires a date column")
        if not isinstance(verified, bool):
            raise ValueError("verified must be an explicit boolean declaration")
        dates = frame["date"].map(_day).tolist()
        if len(set(dates)) != len(dates) or dates != sorted(dates):
            raise ValueError("Calendar dates must be unique and ascending")
        provenance = "user_asserted" if verified else "unverified"
        metadata = {"schema": "kcpredict_calendar_import_v1", "filename": filename,
                    "source_sha256": source_sha, "source": "user_supplied_csv", "provenance": provenance,
                    "externally_verified": False, "verified": verified, "rows": len(dates),
                    "dates_sha256": _hash(dates)}
        import_id = _hash({**metadata, "filename": None})
        inserted, duplicates = [], 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for day in dates:
                existing = connection.execute("SELECT verified, source, provenance FROM market_calendar WHERE date = ?", (day,)).fetchone()
                contract = (int(verified), "user_supplied_csv", provenance)
                if existing is None:
                    inserted.append((day, *contract, import_id))
                elif tuple(existing) == contract:
                    duplicates += 1
                else:
                    raise ValueError(f"Conflicting calendar declaration for {day}; no dates imported")
            if inserted:
                self._record_import(connection, import_id, "calendar", source_sha, metadata)
                connection.executemany("INSERT INTO market_calendar VALUES (?, ?, ?, ?, ?)", inserted)
        return {**metadata, "status": "imported" if inserted else "reused", "import_id": import_id,
                "inserted": len(inserted), "duplicates": duplicates}

    def load_bars(self, symbol: str, end: str | None = None) -> pd.DataFrame:
        canonical = normalize_symbol(symbol)
        sql, parameters = "SELECT * FROM bars WHERE symbol = ?", [canonical]
        if end is not None:
            stamp = pd.Timestamp(end)
            if pd.isna(stamp):
                raise ValueError("Invalid end date")
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
            is_instant = "T" in str(end) or " " in str(end)
            cutoff = stamp.normalize() - pd.Timedelta(days=1) if is_instant and stamp.hour < 15 else stamp.normalize()
            sql += " AND date <= ?"
            parameters.append(cutoff.date().isoformat())
        with self._connection() as connection:
            rows = connection.execute(sql + " ORDER BY date", parameters).fetchall()
            if not rows:
                raise ValueError(f"No local daily bars for {canonical} at the requested end")
            import_ids = sorted({row["import_id"] for row in rows})
            imports = [json.loads(connection.execute("SELECT metadata_json FROM imports WHERE id = ?", (key,)).fetchone()[0])
                       for key in import_ids]
        frame = pd.DataFrame([dict(row) for row in rows]).drop(columns="import_id")
        frame["date"] = pd.to_datetime(frame["date"])
        frame.attrs = {key: _unique_contract(frame[key]) for key in ("price_basis", "volume_unit", "amount_unit", "provenance")}
        frame.attrs.update(schema="kcpredict_local_bars_v1", externally_verified=False,
                           source_files=sorted({item["source_sha256"] for item in imports}),
                           imports=imports)
        return frame

    def calendar(self) -> tuple[list[str], dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM market_calendar ORDER BY date").fetchall()
            import_ids = sorted({row["import_id"] for row in rows})
            imports = [json.loads(connection.execute("SELECT metadata_json FROM imports WHERE id = ?", (key,)).fetchone()[0])
                       for key in import_ids]
        dates = [row["date"] for row in rows]
        declared = bool(rows) and all(row["verified"] == 1 for row in rows)
        contract = [{key: row[key] for key in ("date", "verified", "source", "provenance")} for row in rows]
        identity = {"schema": "kcpredict_calendar_v1", "verified": declared,
                    "source": "user_supplied_csv", "provenance": "user_asserted" if declared else "unverified",
                    "externally_verified": False, "dates_sha256": _hash(dates),
                    "contract_sha256": _hash(contract), "rows": len(dates),
                    "source_sha256": sorted({item["source_sha256"] for item in imports}),
                    "imports": imports}
        return dates, identity

    def symbols(self) -> list[str]:
        with self._connection() as connection:
            return [row[0] for row in connection.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]
