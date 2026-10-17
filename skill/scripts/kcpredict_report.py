#!/usr/bin/env python3
"""Build a local KCPredict interactive HTML report and PNG/SVG previews.

The orchestrator uses only Python's standard library. Runtime discovery belongs
to the sibling kcpredict.py dispatcher; chart dependencies belong to the chosen
chart interpreter. No step downloads data or models or retries changed settings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any


SCRIPTS = Path(__file__).resolve().parent
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


class WorkflowError(RuntimeError):
    """A required stage failed; previously written artifacts are preserved."""


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat()


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkflowError(f"Cannot read expected JSON artifact {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"Expected a JSON object in {path}")
    return data


def _chart_interpreter(explicit: str | None) -> str:
    if explicit is None:
        return sys.executable
    if not explicit.strip():
        raise ValueError("--chart-python must be a single executable path")
    candidate = Path(explicit).expanduser()
    if not candidate.is_file():
        found = shutil.which(str(candidate))
        if not found:
            raise ValueError("--chart-python executable does not exist; the explicit interpreter is not replaced")
        candidate = Path(found)
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise ValueError("--chart-python file is not executable")
    # Preserve a selected virtual environment's Python symlink.
    return str(candidate.absolute())


def _output_directory(requested: Path | None, symbol: str) -> Path:
    if requested is not None:
        location = requested.expanduser().absolute()
        if location.exists():
            if not location.is_dir() or any(location.iterdir()):
                raise ValueError("--output-dir must be a new directory or an existing empty directory; no files were removed")
        else:
            location.mkdir(parents=True)
        return location
    parent = Path.cwd() / "outputs"
    parent.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9_-]", "_", symbol)[:80] or "symbol"
    prefix = safe_symbol + "-" + datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S") + "-"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).absolute()


def _reason(step: dict) -> str:
    message = step.get("stderr", "").strip() or step.get("stdout", "").strip()
    if message:
        return message
    return f"{step['name']} exited with code {step['exit_code']}"


def _run(manifest: dict, name: str, argv: list[str], output: Path | None = None) -> dict:
    step = {"name": name, "argv": argv, "started_at": _now(),
            "output": str(output) if output is not None else None}
    manifest["steps"].append(step)
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        step.update(exit_code=completed.returncode, stdout=completed.stdout or "", stderr=completed.stderr or "")
    except OSError as exc:
        step.update(exit_code=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
    step["completed_at"] = _now()
    if step["exit_code"] in (130, -2):
        raise KeyboardInterrupt
    return step


def _runtime_argv(options, output: Path, command: str, *arguments: str) -> list[str]:
    argv = [sys.executable, str(SCRIPTS / "kcpredict.py")]
    if options.home is not None:
        argv += ["--home", str(options.home.expanduser().absolute())]
    return [*argv, "--output", str(output), command, *arguments]


def _research(path: Path, *, prediction: bool = False) -> dict:
    data = _read_json(path)
    key = "prediction" if prediction else "analysis"
    if data.get("schema") != "kcpredict_research_v1" or not isinstance(data.get(key), dict):
        raise WorkflowError(f"{path} does not contain a kcpredict_research_v1 {key}")
    if prediction:
        quality = data[key].get("quality")
        if not isinstance(quality, dict) or not isinstance(quality.get("prediction_usable"), bool):
            raise WorkflowError(f"{path} has no explicit prediction.quality.prediction_usable")
    elif data[key].get("schema") != "kcpredict_structure_v1" or not data.get("data_end"):
        raise WorkflowError(f"{path} has no structure schema or actual data_end")
    return data


def _placeholder(options, structure: dict, parameters: dict, *, status: str,
                 reason: str, step: dict | None = None) -> dict:
    return {"schema": "kcpredict_prediction_unavailable_v1", "status": status,
            "symbol": structure.get("symbol", options.symbol), "requested_end": options.end,
            "data_end": structure["data_end"], "as_of": structure["analysis"].get("as_of", structure["data_end"]),
            "reason": reason,
            "reason_codes": ["prediction_not_requested" if status == "not_executed" else "prediction_execution_failed"],
            "parameters": parameters,
            "command": ({key: step.get(key) for key in ("argv", "exit_code", "stderr", "stdout")} if step else None),
            "qualification": {"research_only": True, "prediction_usable": False}}


def _artifacts(folder: Path) -> list[dict]:
    return [{"path": str(path.absolute()), "bytes": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(folder.rglob("*")) if path.is_file() and path.name != "manifest.json"]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--symbol", required=True)
    root.add_argument("--end", help="Optional runtime cutoff; preserved exactly when supplied")
    root.add_argument("--home", type=Path, help="Independent runtime state directory; forwarded before each subcommand")
    root.add_argument("--output-dir", type=Path, help="New or empty output directory")
    root.add_argument("--months", type=int, default=3, help="Chart history in calendar months; does not change model lookback")
    root.add_argument("--lookback", type=int, choices=(128, 256), default=128)
    root.add_argument("--horizon", type=int, default=5)
    root.add_argument("--samples", type=int, default=64, help="Requested forecast paths; workflow default: 64")
    root.add_argument("--seed", type=int, default=42)
    root.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    root.add_argument("--temperature", type=float, default=.8)
    root.add_argument("--top-p", type=float, default=.9)
    root.add_argument("--no-predict", action="store_true", help="Explicitly request structure and ICT without inference")
    root.add_argument("--chart-python", help="Explicit chart Python executable; no environment fallback")
    root.add_argument("--notes", type=Path, help="Optional manual chart annotations JSON")
    return root


def _validate(options) -> None:
    if not options.symbol.strip():
        raise ValueError("--symbol cannot be empty")
    if options.months < 1:
        raise ValueError("--months must be a positive integer")
    if not 1 <= options.horizon <= 10:
        raise ValueError("--horizon must be in 1–10")
    if not 1 <= options.samples <= 64:
        raise ValueError("--samples must be in 1–64")
    if not 0 <= options.seed < 2 ** 63:
        raise ValueError("--seed must be in 0–2**63-1")
    if not math.isfinite(options.temperature) or not 0 < options.temperature <= 2:
        raise ValueError("--temperature must be finite and in (0, 2]")
    if not math.isfinite(options.top_p) or not 0 < options.top_p <= 1:
        raise ValueError("--top-p must be finite and in (0, 1]")
    if options.notes is not None and not options.notes.expanduser().is_file():
        raise ValueError("--notes must refer to an existing JSON file")


def execute(options) -> tuple[dict, int]:
    _validate(options)
    chart_python = _chart_interpreter(options.chart_python)
    folder = _output_directory(options.output_dir, options.symbol)
    parameters = {key: getattr(options, key) for key in
                  ("lookback", "horizon", "samples", "seed", "device", "temperature", "top_p")}
    manifest = {"schema": "kcpredict_report_manifest_v1", "status": "running", "started_at": _now(),
                "symbol": options.symbol, "requested_end": options.end, "months": options.months,
                "parameters": parameters, "no_predict": options.no_predict,
                "home": str(options.home.expanduser().absolute()) if options.home is not None else None,
                "output_dir": str(folder), "chart_python": chart_python,
                "prediction_status": "not_started", "steps": [], "errors": [], "paths": {}}
    code = 1
    try:
        status_path = folder / "status.json"
        status = _run(manifest, "status", _runtime_argv(options, status_path, "status"), status_path)
        if status["exit_code"] != 0:
            raise WorkflowError("Runtime status failed: " + _reason(status))
        if _read_json(status_path).get("schema") != "kcpredict_status_v1":
            raise WorkflowError("Runtime status did not produce kcpredict_status_v1 JSON")
        manifest["paths"]["status"] = str(status_path)

        structure_path = folder / "structure.json"
        arguments = ["--symbol", options.symbol]
        if options.end is not None:
            arguments += ["--end", options.end]
        analysis_step = _run(manifest, "analyze", _runtime_argv(options, structure_path, "analyze", *arguments), structure_path)
        if analysis_step["exit_code"] != 0:
            raise WorkflowError("Structure analysis failed: " + _reason(analysis_step))
        structure = _research(structure_path)
        manifest.update(symbol=structure.get("symbol", options.symbol), data_end=structure["data_end"],
                        as_of=structure["analysis"].get("as_of", structure["data_end"]))
        manifest["paths"]["structure"] = str(structure_path)

        prediction_path = folder / "prediction.json"
        if options.no_predict:
            prediction_path = folder / "prediction-unavailable.json"
            _write_json(prediction_path, _placeholder(options, structure, parameters, status="not_executed",
                        reason="Prediction was not executed because --no-predict was explicitly requested."))
            manifest["prediction_status"] = "not_executed"
        else:
            # Pin a default cutoff to the successfully analyzed history snapshot.
            predicted_end = options.end if options.end is not None else structure["data_end"]
            predict_args = ["--symbol", options.symbol, "--end", predicted_end]
            for flag, key in (("--lookback", "lookback"), ("--horizon", "horizon"), ("--samples", "samples"),
                              ("--seed", "seed"), ("--device", "device"), ("--temperature", "temperature"), ("--top-p", "top_p")):
                predict_args += [flag, str(parameters[key])]
            step = _run(manifest, "predict", _runtime_argv(options, prediction_path, "predict", *predict_args), prediction_path)
            failure = None
            prediction = None
            if step["exit_code"] in (0, 2):
                try:
                    prediction = _research(prediction_path, prediction=True)
                    if prediction.get("symbol") != structure.get("symbol") or prediction.get("data_end") != structure["data_end"]:
                        raise WorkflowError("Prediction and structure symbol/data_end do not match")
                except WorkflowError as exc:
                    failure = str(exc)
            else:
                failure = _reason(step)
            if failure is not None:
                manifest["prediction_status"] = "unavailable"
                manifest["errors"].append({"stage": "predict", "reason": failure})
                prediction_path = folder / "prediction-unavailable.json"
                _write_json(prediction_path, _placeholder(options, structure, parameters, status="unavailable",
                            reason=failure, step=step))
            else:
                usable = prediction["prediction"]["quality"]["prediction_usable"]
                manifest["prediction_status"] = "usable" if usable else "quality_unusable"
        manifest["paths"]["prediction"] = str(prediction_path)

        ict_path = folder / "ict.json"
        ict_step = _run(manifest, "ict", [sys.executable, str(SCRIPTS / "kcpredict_ict.py"),
                        "--input", str(structure_path), "--output", str(ict_path)], ict_path)
        ict_ok = False
        if ict_step["exit_code"] in (0, 2):
            try:
                ict = _read_json(ict_path)
                if ict.get("schema") != "kcpredict_ict_v1":
                    raise WorkflowError("ICT stage did not produce kcpredict_ict_v1 JSON")
                manifest["ict_status"] = ict.get("status", "completed")
                manifest["paths"]["ict"] = str(ict_path)
                ict_ok = True
            except WorkflowError as exc:
                manifest["errors"].append({"stage": "ict", "reason": str(exc)})
        else:
            manifest["errors"].append({"stage": "ict", "reason": _reason(ict_step)})
        if not ict_ok:
            manifest["ict_status"] = "unavailable"

        chart_args = [chart_python, str(SCRIPTS / "kcpredict_chart.py"), "--input", str(structure_path),
                      "--prediction", str(prediction_path), "--output-dir", str(folder), "--months", str(options.months)]
        if ict_ok:
            chart_args += ["--ict", str(ict_path)]
        if options.notes is not None:
            chart_args += ["--notes", str(options.notes.expanduser().absolute())]
        chart = _run(manifest, "chart", chart_args)
        if chart["exit_code"] != 0:
            message = _reason(chart)
            missing_matplotlib = re.search(r"no module named [\"']matplotlib(?:[.\"']|$)", message, re.IGNORECASE)
            if missing_matplotlib or "matplotlib is required" in message.lower():
                command = [chart_python, "-m", "pip", "install", "matplotlib"]
                shell_command = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
                manifest["chart_dependency_help"] = {"missing_dependency": "matplotlib", "install_argv": command,
                    "install_command": shell_command, "automatic_install_attempted": False}
                message += "\nInstall the chart dependency in the selected environment: " + shell_command
            raise WorkflowError("Chart rendering failed: " + message)
        generated = _artifacts(folder)
        if not any(item["path"].lower().endswith(".png") for item in generated):
            raise WorkflowError("Chart command returned success but produced no PNG artifact")
        if not any(item["path"].lower().endswith(".svg") for item in generated):
            raise WorkflowError("Chart command returned success but produced no SVG artifact")
        chart_data_path = folder / "chart-data.json"
        if not chart_data_path.is_file():
            raise WorkflowError("Chart command returned success but produced no chart-data.json evidence artifact")
        if _read_json(chart_data_path).get("schema") != "kcpredict_chart_data_v1":
            raise WorkflowError("Chart data did not produce kcpredict_chart_data_v1 JSON")
        manifest["paths"]["chart_data"] = str(chart_data_path)
        reserved = {"status.json", "structure.json", "prediction.json", "prediction-unavailable.json", "ict.json"}
        chart_evidence = [item["path"] for item in generated if item["path"].lower().endswith(".json")
                          and Path(item["path"]).name not in reserved]
        if not chart_evidence:
            raise WorkflowError("Chart command returned success but produced no chart evidence JSON artifact")
        for evidence_path in chart_evidence:
            _read_json(Path(evidence_path))
        manifest["charts"] = {"png": [item["path"] for item in generated if item["path"].lower().endswith(".png")],
                              "svg": [item["path"] for item in generated if item["path"].lower().endswith(".svg")],
                              "html": [], "evidence": chart_evidence}
        html_path = folder / "composite.html"
        # The interactive renderer uses only the standard library. It consumes
        # already evaluated chart data and never invokes the model or changes
        # parameters, quality thresholds, sample paths or historical evidence.
        html_step = _run(manifest, "html", [sys.executable, str(SCRIPTS / "kcpredict_html.py"),
                         "--data", str(chart_data_path), "--output", str(html_path)], html_path)
        if html_step["exit_code"] != 0:
            raise WorkflowError("Interactive HTML rendering failed: " + _reason(html_step))
        if not html_path.is_file() or html_path.stat().st_size == 0:
            raise WorkflowError("HTML command returned success but produced no nonempty composite.html artifact")
        try:
            document = html_path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise WorkflowError("HTML artifact is not valid UTF-8") from exc
        if re.search(r"<html(?:\s|>)", document, re.IGNORECASE) is None:
            raise WorkflowError("HTML command returned success but composite.html is not an HTML document")
        html_step.update(bytes=html_path.stat().st_size, sha256=hashlib.sha256(html_path.read_bytes()).hexdigest())
        manifest["charts"]["html"] = [str(html_path)]
        manifest["paths"]["html"] = str(html_path)
        manifest["status"] = "completed"
        code = 0
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["errors"].append({"stage": "workflow", "reason": "Execution was interrupted; no downstream steps were started."})
        raise
    except (WorkflowError, OSError, ValueError, TypeError, KeyError) as exc:
        manifest["status"] = "failed"
        manifest["errors"].append({"stage": "workflow", "reason": str(exc)})
    finally:
        manifest["completed_at"] = _now()
        manifest["artifacts"] = _artifacts(folder)
        manifest["paths"]["manifest"] = str(folder / "manifest.json")
        _write_json(folder / "manifest.json", manifest)
    return manifest, code


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        manifest, code = execute(options)
        result = {"schema": manifest["schema"], "status": manifest["status"],
                  "symbol": manifest["symbol"], "data_end": manifest.get("data_end"),
                  "prediction_status": manifest["prediction_status"], "output_dir": manifest["output_dir"],
                  "manifest": manifest["paths"]["manifest"], "charts": manifest.get("charts", {}),
                  "errors": manifest["errors"]}
        print(json.dumps(result, ensure_ascii=False), file=sys.stdout if code == 0 else sys.stderr)
        return code
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
