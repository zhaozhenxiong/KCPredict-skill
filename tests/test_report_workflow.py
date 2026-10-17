"""Workflow boundaries: exact runtime parameters, graceful prediction and files."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict_report.py"


@pytest.fixture
def report():
    spec = importlib.util.spec_from_file_location("_standalone_report_workflow", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def research(*, prediction=False, usable=False):
    result = {"schema": "kcpredict_research_v1", "symbol": "002436.SZ", "requested_end": "2026-10-16",
              "data_end": "2026-10-16"}
    if prediction:
        result["prediction"] = {"quality": {"prediction_usable": usable}, "forecast": []}
    else:
        result["analysis"] = {"schema": "kcpredict_structure_v1", "as_of": "2026-10-16", "frequencies": {}}
    return result


def fake_runner(monkeypatch, report, *, prediction_code=2, prediction_payload=True,
                chart_code=0, chart_stderr="", chart_evidence=True, status_code=0, analysis_code=0, ict_code=0,
                html_code=0, html_content="<!DOCTYPE html><html><body>Offline fixture</body></html>",
                chart_data_schema="kcpredict_chart_data_v1"):
    calls = []
    def run(argv, **kwargs):
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        assert all(isinstance(part, str) for part in argv)
        calls.append(argv)
        script = Path(argv[1]).name
        code = 0
        stderr = ""
        output = Path(argv[argv.index("--output") + 1]) if "--output" in argv else None
        payload = None
        if script == "kcpredict.py":
            if "status" in argv:
                code, payload = status_code, {"schema": "kcpredict_status_v1"}
                stderr = "frozen engine verification failed" if code else ""
            elif "analyze" in argv:
                code, payload = analysis_code, research()
                stderr = "No local daily bars" if code else ""
            elif "predict" in argv:
                code = prediction_code
                if prediction_payload:
                    payload = research(prediction=True, usable=code == 0)
                if code == 1:
                    stderr = "RuntimeError: fixed Kronos model weights are unavailable"
            else:
                raise AssertionError(argv)
        elif script == "kcpredict_ict.py":
            code = ict_code
            payload = {"schema": "kcpredict_ict_v1", "status": "unavailable" if code == 2 else "completed"}
            stderr = "invalid ICT input" if code == 1 else ""
        elif script == "kcpredict_chart.py":
            code, stderr = chart_code, chart_stderr
            if code == 0:
                folder = Path(argv[argv.index("--output-dir") + 1])
                (folder / "chart.png").write_bytes(b"PNG fixture")
                (folder / "chart.svg").write_text("<svg/>")
                if chart_evidence:
                    (folder / "chart-data.json").write_text(json.dumps({"schema": chart_data_schema}))
        elif script == "kcpredict_html.py":
            code = html_code
            assert "--data" in argv and "--output" in argv
            assert Path(argv[argv.index("--data") + 1]).name == "chart-data.json"
            assert Path(argv[argv.index("--data") + 1]).is_file()
            if html_content is not None and code == 0:
                output.write_text(html_content, encoding="utf-8")
            stderr = "HTML rendering fixture failed" if code else ""
        else:
            raise AssertionError(argv)
        if payload is not None and output is not None and code in (0, 2):
            with output.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream)
        return SimpleNamespace(returncode=code, stdout="", stderr=stderr)
    monkeypatch.setattr(report.subprocess, "run", run)
    return calls


def load_manifest(folder):
    return json.loads((folder / "manifest.json").read_text())


def test_default_workflow_preserves_quality_exit_two_without_retries(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report)
    folder = tmp_path / "report with spaces"
    assert report.main(["--symbol", "002436.SZ", "--end", "2026-10-16", "--output-dir", str(folder)]) == 0
    manifest = load_manifest(folder)
    assert [step["name"] for step in manifest["steps"]] == ["status", "analyze", "predict", "ict", "chart", "html"]
    assert manifest["parameters"] == {"lookback": 128, "horizon": 5, "samples": 64, "seed": 42,
                                       "device": "auto", "temperature": .8, "top_p": .9}
    assert manifest["months"] == 3
    assert manifest["prediction_status"] == "quality_unusable"
    assert manifest["steps"][2]["exit_code"] == 2
    assert len([argv for argv in calls if "predict" in argv]) == 1
    assert calls[2][calls[2].index("--samples") + 1] == "64"
    assert not (folder / "prediction-unavailable.json").exists()
    assert "--prediction" in calls[-2]
    assert Path(calls[-2][calls[-2].index("--prediction") + 1]) == folder / "prediction.json"
    assert calls[-1] == [sys.executable, str(SCRIPT.parent / "kcpredict_html.py"),
                         "--data", str(folder / "chart-data.json"), "--output", str(folder / "composite.html")]
    assert manifest["charts"]["html"] == [str(folder / "composite.html")]
    assert manifest["paths"]["chart_data"] == str(folder / "chart-data.json")
    assert manifest["steps"][-1]["exit_code"] == 0
    assert manifest["steps"][-1]["sha256"] == hashlib.sha256((folder / "composite.html").read_bytes()).hexdigest()
    assert manifest["steps"][-1]["bytes"] == (folder / "composite.html").stat().st_size
    assert manifest["artifacts"] and all(row["sha256"] for row in manifest["artifacts"])


def test_explicit_legacy_sample_count_overrides_new_workflow_default(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report)
    folder = tmp_path / "explicit-32"
    assert report.main(["--symbol", "002436.SZ", "--samples", "32", "--output-dir", str(folder)]) == 0
    manifest = load_manifest(folder)
    assert manifest["parameters"]["samples"] == 32
    assert calls[2][calls[2].index("--samples") + 1] == "32"
    assert sum("predict" in argv for argv in calls) == 1
    assert manifest["prediction_status"] == "quality_unusable"


def test_prediction_dependency_failure_keeps_structure_and_renders_placeholder(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, prediction_code=1, prediction_payload=False)
    folder = tmp_path / "partial"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 0
    manifest = load_manifest(folder)
    assert manifest["prediction_status"] == "unavailable"
    missing = json.loads((folder / "prediction-unavailable.json").read_text())
    assert missing["schema"] == "kcpredict_prediction_unavailable_v1"
    assert missing["status"] == "unavailable"
    assert missing["data_end"] == missing["as_of"] == "2026-10-16"
    assert "weights are unavailable" in missing["reason"]
    assert missing["command"]["exit_code"] == 1
    assert (folder / "structure.json").is_file() and (folder / "ict.json").is_file()
    assert (folder / "chart.png").is_file()
    assert (folder / "composite.html").is_file()
    assert sum("predict" in argv for argv in calls) == 1


def test_explicit_no_predict_never_calls_inference(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report)
    folder = tmp_path / "only-analysis"
    folder.mkdir()
    assert report.main(["--symbol", "002436.SZ", "--no-predict", "--output-dir", str(folder)]) == 0
    assert not any("predict" in argv for argv in calls)
    placeholder = json.loads((folder / "prediction-unavailable.json").read_text())
    assert placeholder["status"] == "not_executed"
    assert placeholder["reason_codes"] == ["prediction_not_requested"]
    assert load_manifest(folder)["prediction_status"] == "not_executed"


def test_all_custom_parameters_home_and_manual_notes_are_forwarded_verbatim(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, prediction_code=0)
    folder = tmp_path / "custom"
    notes = tmp_path / "notes with spaces.json"
    notes.write_text("{}")
    chart_python = tmp_path / "chart env" / "python"
    chart_python.parent.mkdir()
    chart_python.symlink_to(sys.executable)
    state = tmp_path / "state with spaces"
    cutoff = "2026-10-16T15:00:00+08:00"
    assert report.main(["--symbol", "002436.SZ", "--end", cutoff, "--home", str(state),
        "--output-dir", str(folder), "--months", "6", "--lookback", "256", "--horizon", "10",
        "--samples", "16", "--seed", "777", "--device", "cpu", "--temperature", "1.2", "--top-p", ".8",
        "--chart-python", str(chart_python), "--notes", str(notes)]) == 0
    for argv in calls[:3]:
        assert argv[2:4] == ["--home", str(state)]
    prediction = calls[2]
    for flag, expected in [("--end", cutoff), ("--lookback", "256"), ("--horizon", "10"), ("--samples", "16"),
                           ("--seed", "777"), ("--device", "cpu"), ("--temperature", "1.2"), ("--top-p", "0.8")]:
        assert prediction[prediction.index(flag) + 1] == expected
    assert calls[-2][0] == str(chart_python)
    assert calls[-2][calls[-2].index("--notes") + 1] == str(notes)
    assert calls[-1][0] == sys.executable  # HTML itself needs no plotting environment.
    assert load_manifest(folder)["prediction_status"] == "usable"


def test_unspecified_end_is_pinned_to_structure_history_for_prediction(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report)
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(tmp_path / "pin")]) == 0
    assert "--end" not in calls[1]
    assert calls[2][calls[2].index("--end") + 1] == "2026-10-16"


def test_existing_nonempty_output_is_never_modified_or_used(report, monkeypatch, tmp_path, capsys):
    calls = fake_runner(monkeypatch, report)
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.json"
    marker.write_text("unchanged")
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(existing)]) == 1
    assert marker.read_text() == "unchanged" and list(existing.iterdir()) == [marker]
    assert not calls
    assert "empty directory" in json.loads(capsys.readouterr().err)["message"]


def test_default_output_folders_are_unique_and_symbol_cannot_escape(report, monkeypatch, tmp_path):
    fake_runner(monkeypatch, report)
    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        assert report.main(["--symbol", "../../$(unsafe);", "--no-predict"]) == 0
    folders = list((tmp_path / "outputs").iterdir())
    assert len(folders) == 2 and folders[0] != folders[1]
    assert all(folder.parent == tmp_path / "outputs" for folder in folders)


@pytest.mark.parametrize("stage", ["status", "analyze"])
def test_required_stage_failure_stops_downstream_and_preserves_error(report, monkeypatch, tmp_path, stage):
    calls = fake_runner(monkeypatch, report, status_code=1 if stage == "status" else 0,
                        analysis_code=1 if stage == "analyze" else 0)
    folder = tmp_path / stage
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 1
    manifest = load_manifest(folder)
    assert manifest["status"] == "failed"
    assert manifest["steps"][-1]["name"] == stage
    assert not any("predict" in argv for argv in calls)
    assert "failed" in manifest["errors"][-1]["reason"]


def test_matplotlib_failure_reports_install_command_for_selected_python_without_installing(report, monkeypatch, tmp_path, capsys):
    calls = fake_runner(monkeypatch, report, chart_code=1,
                        chart_stderr="ModuleNotFoundError: No module named 'matplotlib'")
    folder = tmp_path / "no-chart-deps"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 1
    manifest = load_manifest(folder)
    help_info = manifest["chart_dependency_help"]
    assert help_info["install_argv"] == [sys.executable, "-m", "pip", "install", "matplotlib"]
    assert help_info["automatic_install_attempted"] is False
    assert not any("pip" in argv for argv in calls)
    assert "pip install matplotlib" in capsys.readouterr().err
    assert (folder / "structure.json").is_file()


def test_ict_execution_failure_still_renders_structure(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, ict_code=1)
    folder = tmp_path / "no-ict"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 0
    assert "--ict" not in calls[-2]
    assert load_manifest(folder)["ict_status"] == "unavailable"
    assert (folder / "chart.png").is_file()


def test_rendered_chart_without_auditable_evidence_is_not_reported_complete(report, monkeypatch, tmp_path):
    fake_runner(monkeypatch, report, chart_evidence=False)
    folder = tmp_path / "missing-chart-evidence"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 1
    manifest = load_manifest(folder)
    assert manifest["status"] == "failed"
    assert "no chart-data.json evidence" in manifest["errors"][-1]["reason"]
    assert (folder / "chart.png").is_file()


def test_chart_data_requires_the_validated_renderer_contract_before_html(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, chart_data_schema="wrong_schema")
    folder = tmp_path / "invalid-chart-data"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 1
    assert "kcpredict_chart_data_v1" in load_manifest(folder)["errors"][-1]["reason"]
    assert not any(Path(argv[1]).name == "kcpredict_html.py" for argv in calls)


@pytest.mark.parametrize("code,content,reason", [
    (1, None, "Interactive HTML rendering failed"),
    (0, None, "no nonempty composite.html"),
    (0, "", "no nonempty composite.html"),
    (0, "plain text", "not an HTML document"),
])
def test_failed_or_missing_html_is_never_reported_complete_but_previews_survive(report, monkeypatch, tmp_path, code, content, reason):
    calls = fake_runner(monkeypatch, report, html_code=code, html_content=content)
    folder = tmp_path / "html-problem"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 1
    manifest = load_manifest(folder)
    assert manifest["status"] == "failed"
    assert reason in manifest["errors"][-1]["reason"]
    assert manifest["steps"][-1]["name"] == "html"
    assert manifest["steps"][-1]["exit_code"] == code
    assert manifest["charts"]["html"] == []
    assert (folder / "chart.png").is_file() and (folder / "chart.svg").is_file()
    assert (folder / "chart-data.json").is_file()
    assert sum("predict" in argv for argv in calls) == 1


def test_interrupted_html_preserves_completed_chart_without_retrying_model(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, html_code=130, html_content=None)
    folder = tmp_path / "html-interrupted"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 130
    manifest = load_manifest(folder)
    assert manifest["status"] == "interrupted"
    assert manifest["steps"][-1]["name"] == "html"
    assert (folder / "chart.png").is_file()
    assert sum("predict" in argv for argv in calls) == 1


def test_interrupted_prediction_stops_downstream_instead_of_rendering_failure(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report, prediction_code=130, prediction_payload=False)
    folder = tmp_path / "interrupted"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder)]) == 130
    manifest = load_manifest(folder)
    assert manifest["status"] == "interrupted"
    assert manifest["steps"][-1]["name"] == "predict"
    assert len(calls) == 3


def test_explicit_invalid_chart_interpreter_never_falls_back(report, monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch, report)
    folder = tmp_path / "not-created"
    assert report.main(["--symbol", "002436.SZ", "--output-dir", str(folder),
                        "--chart-python", str(tmp_path / "missing-python")]) == 1
    assert not calls and not folder.exists()


def test_real_cli_from_unrelated_directory_reports_invalid_explicit_runtime_without_traceback(tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["KCPREDICT_PYTHON"] = str(tmp_path / "unavailable-runtime")
    environment.pop("KCPREDICT_EXECUTABLE", None)
    folder = tmp_path / "real-error-report"
    result = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--symbol", "002436.SZ",
                             "--output-dir", str(folder)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    summary = json.loads(result.stderr)
    assert summary["status"] == "failed"
    manifest = load_manifest(folder)
    assert len(manifest["steps"]) == 1
    assert "KCPREDICT_PYTHON" in manifest["steps"][0]["stderr"]
    assert not (folder / "structure.json").exists()


def test_real_cli_missing_symbol_is_argparse_error(tmp_path):
    result = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT)], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "--symbol" in result.stderr and "Traceback" not in result.stderr
