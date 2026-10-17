"""Exercise standalone entry points, request admission and explicit output."""
import argparse
from datetime import datetime
import http.client
import json
from pathlib import Path
import threading

import pytest

from kcpredict import cli
from kcpredict.server import Handler, ThreadingHTTPServer, serve


def test_status_uses_own_empty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path))
    result = cli.status()
    assert result["home"] == str(tmp_path)
    assert result["symbols"] == []
    assert not result["models"]["ready"]
    assert not result["calendar"]["identity"]["verified"]
    assert not result["runtime_project_dependency"]


def test_output_is_exclusive_and_keeps_quality_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "execute", lambda _: {"prediction": {"quality": {"prediction_usable": False}}})
    output = tmp_path / "result.json"
    assert cli.main(["--output", str(output), "status"]) == 2
    before = output.read_bytes()
    assert cli.main(["--output", str(output), "status"]) == 1
    assert output.read_bytes() == before
    assert "FileExistsError" in capsys.readouterr().err


def test_unverified_calendar_never_passes_structure_admission(monkeypatch):
    import pandas as pd
    import kcpredict.data as data
    import kcpredict.structure as structure
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-10-16"])})
    class FakeStore:
        def load_bars(self, *_, **__): return frame
        def calendar(self): return ["2026-10-16"], {"verified": False}
    monkeypatch.setattr(data, "Store", FakeStore)
    def analyze(frame, **kwargs):
        assert kwargs["market_dates"] is None
        return {"status": "unavailable"}
    monkeypatch.setattr(structure, "analyze", analyze)
    args = cli.parser().parse_args(["analyze", "--symbol", "002436.SZ", "--end", "2026-10-16"])
    assert cli.research(args)["analysis"]["status"] == "unavailable"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()
    server.server_close()
    thread.join()


def call(port, method, path, body=None, headers=None):
    client = http.client.HTTPConnection("127.0.0.1", port)
    client.request(method, path, body=body, headers=headers or {})
    response = client.getresponse()
    result = (response.status, response.read())
    client.close()
    return result


def test_web_serves_bundled_offline_ui_and_status(web):
    code, body = call(web, "GET", "/")
    assert code == 200 and b"KCPredict" in body
    assert b"cdn." not in body and b"8124" not in body
    code, body = call(web, "GET", "/api/status")
    assert code == 200 and json.loads(body)["symbols"] == []


def test_web_rejects_external_origin_host_and_large_payload(web):
    assert call(web, "GET", "/api/status", headers={"Host": "attacker.example"})[0] == 403
    assert call(web, "POST", "/api/import", "{}", {"Origin": "https://attacker.example"})[0] == 403
    assert call(web, "POST", "/api/import", headers={"Content-Length": "20971521"})[0] == 413
    assert call(web, "POST", "/api/research", json.dumps({"command": "models"}))[0] == 400


def test_server_rejects_public_binding():
    with pytest.raises(ValueError):
        serve("0.0.0.0", 0)


def test_web_calendar_upload_preserves_explicit_declaration(web):
    payload = {"kind": "calendar", "csv": "date\n2026-10-16\n2026-10-19\n", "verified": False}
    code, body = call(web, "POST", "/api/import", json.dumps(payload))
    assert code == 200 and not json.loads(body)["verified"]
    code, body = call(web, "GET", "/api/status")
    assert not json.loads(body)["calendar"]["identity"]["verified"]


def test_forecast_future_dates_follow_shanghai_day_of_timezone_timestamp(monkeypatch):
    import pandas as pd
    import kcpredict.data as data
    import kcpredict.prediction as prediction
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-10-16", "2026-10-19"])})
    calendar = ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
    class FakeStore:
        def load_bars(self, *_, **__): return frame
        def calendar(self): return calendar, {"verified": True}
    monkeypatch.setattr(data, "Store", FakeStore)
    def predict(frame, future, **kwargs):
        # Sunday 23:00 UTC-08 is Monday 15:00 Shanghai. Monday is
        # the actual input close and must not be selected as a future date.
        assert kwargs["as_of"] == "2026-10-18T23:00:00-08:00"
        assert future == calendar[2:]
        assert kwargs["calendar_verified"]
        return {"quality": {"prediction_usable": True}}
    monkeypatch.setattr(prediction, "predict", predict)
    args = cli.parser().parse_args(["predict", "--symbol", "002436.SZ", "--end", "2026-10-18T23:00:00-08:00"])
    assert cli.research(args)["prediction"]["quality"]["prediction_usable"]


def test_declared_calendar_cannot_admit_missing_history_days(monkeypatch):
    import pandas as pd
    import kcpredict.data as data
    import kcpredict.prediction as prediction
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-10-15", "2026-10-16"])})
    class FakeStore:
        def load_bars(self, *_, **__): return frame
        def calendar(self): return ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"], {"verified": True}
    monkeypatch.setattr(data, "Store", FakeStore)
    def predict(frame, future, **kwargs):
        assert kwargs["calendar_verified"] is False
        return {"quality": {"prediction_usable": False}}
    monkeypatch.setattr(prediction, "predict", predict)
    args = cli.parser().parse_args(["predict", "--symbol", "002436.SZ", "--end", "2026-10-16"])
    result = cli.research(args)
    assert result["prediction_calendar_validation"] == {
        "declared_verified": True, "history_coverage_complete": False,
        "missing_history_dates": ["2026-10-15"], "admitted": False}


@pytest.mark.parametrize("hour,expected_cutoff,expected_as_of", [
    (14, "2026-10-18", "2026-10-16"), (15, "2026-10-19", "2026-10-19"),
])
def test_default_forecast_uses_last_available_close_before_current_cutoff(monkeypatch, hour, expected_cutoff, expected_as_of):
    import pandas as pd
    import kcpredict.data as data
    import kcpredict.prediction as prediction
    calendar = ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 10, 19, hour, 0, tzinfo=tz)
    class FakeStore:
        def load_bars(self, symbol, end=None):
            assert end == expected_cutoff
            return pd.DataFrame({"date": pd.to_datetime([day for day in calendar[:2] if day <= end])})
        def calendar(self): return calendar, {"verified": True}
    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    monkeypatch.setattr(data, "Store", FakeStore)
    def predict(frame, future, **kwargs):
        assert kwargs["as_of"] == expected_as_of
        assert len(future) == 5 and future[0] > expected_as_of
        return {"quality": {"prediction_usable": True}}
    monkeypatch.setattr(prediction, "predict", predict)
    args = cli.parser().parse_args(["predict", "--symbol", "002436.SZ"])
    assert cli.research(args)["data_end"] == expected_as_of


def test_explicit_intraday_end_is_preserved_for_original_close_rejection(monkeypatch):
    import numpy as np
    import pandas as pd
    import kcpredict.data as data
    import kcpredict.prediction as prediction
    dates = pd.bdate_range(end="2026-10-19", periods=16)
    frame = pd.DataFrame({"date": dates, "open": 10., "high": 11., "low": 9.,
                          "close": 10., "volume": 100., "amount": 1000.})
    future = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
    class FakeStore:
        def load_bars(self, *_, **__): return frame
        def calendar(self): return list(dates.strftime("%Y-%m-%d")) + future, {"verified": True}
    monkeypatch.setattr(data, "Store", FakeStore)
    def predict(frame, future, **kwargs):
        return prediction.prepare_input(frame, future, symbol=kwargs["symbol"],
            as_of=kwargs["as_of"], lookback=16, horizon=5)
    monkeypatch.setattr(prediction, "predict", predict)
    args = cli.parser().parse_args(["predict", "--symbol", "002436.SZ", "--end", "2026-10-19T14:59:59+08:00"])
    with pytest.raises(ValueError, match="after 15"):
        cli.research(args)


@pytest.mark.parametrize("payload", [[], None, "not-an-object", 1])
def test_web_rejects_nonobject_json_with_a_json_error(web, payload):
    code, body = call(web, "POST", "/api/research", json.dumps(payload))
    assert code == 400 and json.loads(body)["error"] == "Request JSON must be an object"
