"""The portable skill selects only authorized runtimes and preserves CLI results."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "kcpredict.py"


@pytest.fixture
def dispatcher(monkeypatch):
    monkeypatch.delenv("KCPREDICT_PYTHON", raising=False)
    monkeypatch.delenv("KCPREDICT_EXECUTABLE", raising=False)
    spec = importlib.util.spec_from_file_location("_standalone_skill_dispatcher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_python_takes_precedence_and_returns_quality_failure(dispatcher, monkeypatch):
    monkeypatch.setenv("KCPREDICT_PYTHON", sys.executable)
    monkeypatch.setenv("KCPREDICT_EXECUTABLE", "/invalid/unused/runtime")
    invocation = {}
    def run(arguments, **kwargs):
        invocation.update(arguments=arguments, kwargs=kwargs)
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    arguments = ["--output", "a report with spaces.json", "run", "--symbol", "002436.SZ",
                 "--samples", "8", "--seed", "42"]
    assert dispatcher.main(arguments) == 2
    assert invocation["arguments"] == [str(Path(sys.executable).absolute()), "-m", "kcpredict", *arguments]
    assert invocation["kwargs"] == {"check": False}


def test_explicit_cli_path_with_spaces_is_one_argument(dispatcher, monkeypatch, tmp_path):
    executable = tmp_path / "KCPredict installation" / "kcpredict"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("KCPREDICT_EXECUTABLE", str(executable))
    assert dispatcher.resolve_command() == [str(executable.absolute())]


def test_explicit_python_preserves_virtual_environment_symlink(dispatcher, monkeypatch, tmp_path):
    interpreter = tmp_path / "isolated-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    try:
        interpreter.symlink_to(sys.executable)
    except OSError:
        pytest.skip("Creating interpreter symlinks requires unavailable platform privileges")
    monkeypatch.setenv("KCPREDICT_PYTHON", str(interpreter))
    assert dispatcher.resolve_command() == [str(interpreter), "-m", "kcpredict"]


@pytest.mark.parametrize("variable", ["KCPREDICT_PYTHON", "KCPREDICT_EXECUTABLE"])
def test_bad_explicit_override_never_falls_back(dispatcher, monkeypatch, variable):
    monkeypatch.setenv(variable, "/does/not/exist/kcpredict-runtime")
    monkeypatch.setattr(dispatcher.shutil, "which", lambda value: None)
    monkeypatch.setattr(dispatcher, "_current_package_available", lambda: True)
    with pytest.raises(dispatcher.KCPredictUnavailable):
        dispatcher.resolve_command()


def test_path_runtime_is_preferred_to_current_python(dispatcher, monkeypatch, tmp_path):
    executable = tmp_path / "kcpredict"
    monkeypatch.setattr(dispatcher.shutil, "which", lambda value: str(executable) if value == "kcpredict" else None)
    monkeypatch.setattr(dispatcher, "_current_package_available", lambda: True)
    assert dispatcher.resolve_command() == [str(executable)]


def test_current_python_module_is_the_last_available_destination(dispatcher, monkeypatch):
    monkeypatch.setattr(dispatcher.shutil, "which", lambda value: None)
    monkeypatch.setattr(dispatcher, "_current_package_available", lambda: True)
    assert dispatcher.resolve_command() == [sys.executable, "-m", "kcpredict"]


def test_no_runtime_reports_installation_without_spawning(dispatcher, monkeypatch, capsys):
    monkeypatch.setattr(dispatcher.shutil, "which", lambda value: None)
    monkeypatch.setattr(dispatcher, "_current_package_available", lambda: False)
    def unexpected(*args, **kwargs):
        raise AssertionError("No process may run without a selected runtime")
    monkeypatch.setattr(dispatcher.subprocess, "run", unexpected)
    assert dispatcher.main(["status"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "KCPredictUnavailable"
    assert "install.py" in error["message"]


def test_recursive_explicit_dispatch_is_rejected(dispatcher, monkeypatch):
    monkeypatch.setenv("KCPREDICT_EXECUTABLE", str(SCRIPT))
    with pytest.raises(dispatcher.KCPredictUnavailable):
        dispatcher.resolve_command()


@pytest.mark.parametrize("explicit_python", [True, False])
def test_standalone_cli_executes_from_an_unrelated_directory(tmp_path, explicit_python):
    environment = dict(os.environ)
    environment.pop("KCPREDICT_PYTHON", None)
    environment.pop("KCPREDICT_EXECUTABLE", None)
    environment["KCPREDICT_HOME"] = str(tmp_path / "private-state")
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PATH"] = str(tmp_path / "no-executables")
    if explicit_python:
        environment["KCPREDICT_PYTHON"] = sys.executable
    result = subprocess.run([sys.executable, str(SCRIPT), "--version"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.0.4"
    assert not (tmp_path / "private-state").exists()
