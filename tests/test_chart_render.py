"""Real renderer smoke checks for arbitrary instruments and every forecast state."""
import copy
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/"skill"/"scripts"


@pytest.fixture
def chart(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec=importlib.util.spec_from_file_location("_chart_render_test",SCRIPTS/"kcpredict_chart.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Every build host can render this portable fallback without a CJK font.
    monkeypatch.setattr(module,"configure_font",lambda *args:("DejaVu Sans",False))
    return module


def source(symbol="600000.SH",base=10.0):
    first=date(2024,2,1)
    rows=[{"time":(first+timedelta(days=i)).isoformat(),"open":base+i*.01,
        "high":base+1+i*.01,"low":base-1+i*.01,"close":base+.2+i*.01,
        "volume":1_000_000+i*200,"is_closed":True} for i in range(80)]
    return {"schema":"kcpredict_structure_v1","symbol":symbol,"as_of":rows[-1]["time"],
        "data_end":rows[-1]["time"],"price_basis":"unadjusted",
        "frequencies":{"日线":{"bars":rows,"pens":[],"zones":[]}}}


def forecast(payload,status):
    import numpy as np
    if status in ("unavailable","not_executed"):
        return {"schema":"kcpredict_prediction_unavailable_v1","symbol":payload["symbol"],
            "as_of":payload["as_of"],"data_end":payload["data_end"],"status":status,
            "reason":"Model weights have not been imported." if status=="unavailable" else "User requested analysis only.",
            "parameters":{"horizon":5,"samples":32}}
    last=payload["frequencies"]["日线"]["bars"][-1]
    count,valid= (8,8) if status=="pass" else (10,7)
    paths=[[[last["close"]+.1+j*.1+i*.01,last["close"]+.5+j*.1+i*.01,
        last["close"]-.2+j*.1+i*.01,last["close"]+.2+j*.1+i*.01,2_000_000,20_000_000]
        for j in range(5)] for i in range(count)]
    for path in paths[valid:]:
        path[0][1]=path[0][2]-1
    quantiles=np.quantile(np.asarray(paths[:valid]),[.1,.5,.9],axis=0)
    days=[(date.fromisoformat(payload["data_end"])+timedelta(days=i+1)).isoformat() for i in range(5)]
    result=[]
    for i,day in enumerate(days):
        row={key:float(quantiles[1,i,j]) for j,key in enumerate(("open","high","low","close","volume","amount"))}
        row.update(date=day,close_p10=float(quantiles[0,i,3]),close_p90=float(quantiles[2,i,3]))
        result.append(row)
    return {"schema":"kronos_forecast_v1","symbol":payload["symbol"],"as_of":payload["as_of"],"status":"completed",
        "input":{"symbol":payload["symbol"],"as_of":payload["as_of"],"history_end":payload["data_end"],
            "last_close":last["close"],"horizon":5,"price_basis":"unadjusted"},
        "quality":{"requested_samples":count,"valid_samples":valid,"invalid_samples":count-valid,
            "valid_fraction":valid/count,"valid_mask":[i<valid for i in range(count)],
            "minimum_valid_samples":8,"minimum_valid_fraction":.8,"prediction_usable":status=="pass"},
        "identity":{"future_dates":days,"parameters":{"samples":count,"horizon":5}},
        "samples":paths,"forecast":result,"summary":None}


@pytest.mark.parametrize("status",["pass","fail","unavailable","not_executed"])
def test_real_plot_preserves_prediction_state_and_exports(chart,tmp_path,status):
    pytest.importorskip("matplotlib")
    from PIL import Image
    raw=source()
    data=chart.prepare_chart_data(raw,prediction_payload=forecast(raw,status))
    before=copy.deepcopy(data["prediction"])
    result=chart.render(data,tmp_path,dpi=72)
    assert result["prediction_status"]==status
    assert result["prediction_usable"] is (status=="pass")
    evidence=json.loads(Path(result["evidence"]).read_text())
    assert evidence["prediction"]==before
    assert evidence["meta"]["symbol"]=="600000.SH"
    assert evidence["meta"]["as_of"]==raw["as_of"]
    assert evidence["render"]["language"]=="en"
    assert evidence["quality"]["structure_status"]=="unavailable"
    with Image.open(result["png"]) as image:
        assert image.size[0]>=1000 and image.size[1]>=700
        image.verify()
    assert Path(result["svg"]).stat().st_size>10_000


def test_different_symbol_and_price_scale_are_used_and_existing_files_survive(chart,tmp_path):
    pytest.importorskip("matplotlib")
    raw=source("000333.SZ",base=100.0)
    data=chart.prepare_chart_data(raw)
    result=chart.render(data,tmp_path,dpi=72)
    first=Path(result["png"]).read_bytes()
    with pytest.raises(FileExistsError):
        chart.render(data,tmp_path,dpi=72)
    assert Path(result["png"]).read_bytes()==first
    stored=json.loads(Path(result["evidence"]).read_text())
    assert stored["meta"]["symbol"]=="000333.SZ"
    assert stored["history"]["bars"][-1]["close"]>100
    assert "002436" not in json.dumps(stored)


def test_missing_plot_dependency_has_actionable_error_without_outputs(chart,monkeypatch,tmp_path):
    data=chart.prepare_chart_data(source())
    monkeypatch.setitem(sys.modules,"matplotlib",None)
    with pytest.raises(RuntimeError,match="matplotlib is required"):
        chart.render(data,tmp_path)
    assert list(tmp_path.iterdir())==[]


def test_history_forecast_and_volume_use_one_continuous_axis_without_resampling(chart,monkeypatch,tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib.pyplot as plt
    import numpy as np

    figures=[]
    original=plt.figure
    def capture(*args,**kwargs):
        figure=original(*args,**kwargs)
        figures.append(figure)
        return figure
    monkeypatch.setattr(plt,"figure",capture)
    raw=source()
    data=chart.prepare_chart_data(raw,prediction_payload=forecast(raw,"pass"))
    expected=copy.deepcopy(data["prediction"])
    result=chart.render(data,tmp_path,dpi=72)
    figure=figures[-1]
    assert len(figure.axes)==3  # price, shared volume, horizontal quality strip
    price,volume,quality=figure.axes
    assert price.get_position().width>.85
    assert price.get_shared_x_axes().joined(price,volume)
    assert price.get_xlim()==volume.get_xlim()
    assert quality.get_position().y0<volume.get_position().y0
    n=len(data["history"]["bars"])
    horizon=len(expected["forecast"])
    x=list(range(n-1,n+horizon))
    last=data["history"]["bars"][-1]["close"]
    median=next(line for line in price.lines if line.get_gid()=="forecast_median")
    np.testing.assert_array_equal(median.get_xdata(),x)
    np.testing.assert_array_equal(median.get_ydata(),[last,*[row["close"] for row in expected["forecast"]]])
    for index,path in enumerate(expected["samples"]):
        sample=next(line for line in price.lines if line.get_gid()==f"forecast_sample_{index}")
        np.testing.assert_array_equal(sample.get_xdata(),x)
        np.testing.assert_array_equal(sample.get_ydata(),[last,*[row[3] for row in path]])
    band=next(collection for collection in price.collections if collection.get_gid()=="forecast_quantile_band")
    vertices=band.get_paths()[0].vertices
    for i,row in enumerate(expected["forecast"],start=n):
        for value in (row["close_p10"],row["close_p90"]):
            assert np.any(np.isclose(vertices[:,0],i)&np.isclose(vertices[:,1],value))
    future_volume=[rectangle for rectangle in volume.patches if rectangle.get_gid()=="forecast_volume"]
    assert len(future_volume)==horizon
    np.testing.assert_allclose([rectangle.get_x()+rectangle.get_width()/2 for rectangle in future_volume],range(n,n+horizon))
    np.testing.assert_array_equal([rectangle.get_height() for rectangle in future_volume],
                                 [row["volume"]/1e8 for row in expected["forecast"]])
    for axis in (price,volume):
        boundary=next(line for line in axis.lines if line.get_gid()=="prediction_boundary")
        np.testing.assert_array_equal(boundary.get_xdata(),[n-.5,n-.5])
        assert any(patch.get_gid()=="forecast_region" for patch in axis.patches)
    evidence=json.loads(Path(result["evidence"]).read_text())
    assert evidence["prediction"]==expected
    assert evidence["render"]["layout"]=="single_continuous_timeline"
    assert evidence["render"]["timeline"]=={"historical_x":[0,n-1],"forecast_anchor_x":n-1,
                                           "forecast_x":list(range(n,n+horizon)),"volume_axis_shared":True}
