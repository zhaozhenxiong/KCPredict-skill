"""Portable command line. All data and weights belong to KCPredict's own home."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from . import __version__


def state_home():
    return Path(os.environ.get("KCPREDICT_HOME") or Path.home() / ".kcpredict").expanduser().resolve()


def status():
    from .data import Store
    from .engine import engine_status
    from .model_assets import asset_status
    store = Store()
    dates, identity = store.calendar()
    return {"schema": "kcpredict_status_v1", "version": __version__, "home": str(state_home()),
            "engine": engine_status(), "symbols": store.symbols(),
            "calendar": {"sessions": len(dates), "start": dates[0] if dates else None,
                         "end": dates[-1] if dates else None, "identity": identity},
            "prediction_dependencies_installed": all(importlib.util.find_spec(name) is not None
                 for name in ("torch", "einops", "safetensors", "tqdm")),
            "models": asset_status(), "runtime_project_dependency": False}


def research(options):
    from .data import Store
    from ._structure_contract import normalize_symbol
    store = Store()
    symbol = normalize_symbol(options.symbol)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    default_end = (now.date() if now.hour >= 15 else now.date() - timedelta(days=1)).isoformat()
    end = options.end or default_end
    frame = store.load_bars(symbol, end=end)
    if frame.empty:
        raise ValueError("该股票在截止时间前没有已导入行情")
    last = frame.date.max().strftime("%Y-%m-%d")
    dates, calendar_identity = store.calendar()
    verified = bool(calendar_identity.get("verified"))
    result = {"schema": "kcpredict_research_v1", "symbol": symbol,
              "requested_end": options.end, "data_end": last,
              "data_provenance": frame.attrs, "calendar_identity": calendar_identity}
    if options.command in ("analyze", "run"):
        from .structure import analyze
        result["analysis"] = analyze(frame, symbol=symbol, as_of=end,
                    start=getattr(options, "start", None), market_dates=dates if verified else None)
    if options.command in ("predict", "run"):
        import pandas as pd
        from .prediction import predict
        # An explicitly requested day must exist; never replace it with an older close.
        prediction_end = options.end or last
        cutoff = pd.Timestamp(prediction_end)
        if pd.isna(cutoff):
            raise ValueError("Invalid prediction end date")
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_convert("Asia/Shanghai").tz_localize(None)
        cutoff = cutoff.normalize()
        cutoff_day = cutoff.date().isoformat()
        future = [day for day in dates if day > cutoff_day][:options.horizon]
        # A calendar declaration alone cannot admit a prediction whose actual
        # input window contains dates absent from that supplied calendar.
        history = frame.loc[pd.to_datetime(frame.date) <= cutoff, "date"].tail(options.lookback)
        history_dates = {pd.Timestamp(day).date().isoformat() for day in history}
        missing_dates = sorted((history_dates | {cutoff_day}) - set(dates))
        admitted = verified and not missing_dates
        result["prediction_calendar_validation"] = {
            "declared_verified": verified, "history_coverage_complete": not missing_dates,
            "missing_history_dates": missing_dates, "admitted": admitted}
        result["prediction"] = predict(frame, future, symbol=symbol, as_of=prediction_end,
               lookback=options.lookback, horizon=options.horizon, samples=options.samples,
               seed=options.seed, device=options.device, temperature=options.temperature,
               top_p=options.top_p, calendar_identity=calendar_identity,
               calendar_verified=admitted)
    return result


def execute(options):
    from .data import Store
    if options.command == "status":
        return status()
    if options.command == "data":
        if options.data_command == "import-csv":
            return Store().import_csv(options.file, symbol=options.symbol,
                    verified_unadjusted=options.verified_unadjusted,
                    volume_unit=options.volume_unit, amount_unit=options.amount_unit)
        if options.data_command == "import-calendar":
            return Store().import_calendar(options.file, verified=options.verified)
        return {"symbols": Store().symbols()}
    if options.command == "models":
        from .model_assets import asset_status, prepare_assets
        if options.models_command == "status":
            return asset_status()
        return prepare_assets(source=options.directory if options.models_command == "import" else None,
                   download=options.models_command == "download", allow_base_download=options.models_command == "download")
    return research(options)


def parser():
    root = argparse.ArgumentParser(prog="kcpredict", description="独立 CZSC 结构与 Kronos 路径研究")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", help="独立状态目录，覆盖 KCPREDICT_HOME")
    root.add_argument("--output", type=Path, help="把完整 JSON 保存到新文件（不覆盖已有文件）")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="检查引擎、独立数据与模型")
    data = commands.add_parser("data", help="导入自己的 CSV 日线与交易日历").add_subparsers(dest="data_command", required=True)
    bars = data.add_parser("import-csv")
    bars.add_argument("file", type=Path)
    bars.add_argument("--symbol")
    bars.add_argument("--verified-unadjusted", action="store_true", help="明确声明已核验的未复权行情契约；来源仍为用户声明")
    bars.add_argument("--volume-unit", choices=("shares",), default="shares")
    bars.add_argument("--amount-unit", choices=("CNY",), default="CNY")
    calendar = data.add_parser("import-calendar")
    calendar.add_argument("file", type=Path)
    calendar.add_argument("--verified", action="store_true", help="明确声明日历已经核验；不会自行认证外部来源")
    data.add_parser("list")
    models = commands.add_parser("models", help="独立权重管理").add_subparsers(dest="models_command", required=True)
    models.add_parser("status")
    models.add_parser("download", help="显式下载固定版本模型，约 425 MB")
    imp = models.add_parser("import")
    imp.add_argument("directory", type=Path)
    for name in ("analyze", "predict", "run"):
        command = commands.add_parser(name)
        command.add_argument("--symbol", required=True)
        command.add_argument("--end", help="YYYY-MM-DD，或结构分析的带时区截止时间")
        if name in ("analyze", "run"):
            command.add_argument("--start", help="只截取展示起点，保留前置结构预热")
        if name in ("predict", "run"):
            command.add_argument("--lookback", type=int, choices=(128, 256), default=128)
            command.add_argument("--horizon", type=int, default=5)
            command.add_argument("--samples", type=int, default=8)
            command.add_argument("--seed", type=int, default=42)
            command.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
            command.add_argument("--temperature", type=float, default=.8)
            command.add_argument("--top-p", type=float, default=.9)
    serve = commands.add_parser("serve", help="打开完全本地的独立界面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8130)
    return root


def main(argv=None):
    options = parser().parse_args(argv)
    if options.home:
        os.environ["KCPREDICT_HOME"] = str(Path(options.home).expanduser().resolve())
    try:
        if options.command == "serve":
            from .server import serve
            serve(options.host, options.port)
            return 0
        result = execute(options)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n"
        if options.output:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            with options.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
            print(json.dumps({"saved": str(options.output.resolve()), "data_end": result.get("data_end")}, ensure_ascii=False))
        else:
            print(encoded, end="")
        prediction = result.get("prediction")
        return 2 if prediction and not prediction["quality"]["prediction_usable"] else 0
    except (ValueError, RuntimeError, FileNotFoundError, ImportError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
