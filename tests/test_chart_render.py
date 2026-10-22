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
    assert evidence["render"]["theme"]==chart.theme_identity(chart.load_theme())
    assert result["theme"]==evidence["render"]["theme"]
    assert evidence["render"]["layer_defaults"]["samples"] is False
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
    raw["frequencies"]["日线"]["bars"][0]["open"] += .4  # Exercise both candle directions.
    data=chart.prepare_chart_data(raw,prediction_payload=forecast(raw,"pass"))
    bars=data["history"]["bars"]
    data["pens"]=[{"start_time":bars[5]["time"],"end_time":bars[15]["time"],
                   "start_price":10.1,"end_price":11.2,"display_confirmed":True},
                  {"start_time":bars[65]["time"],"end_time":bars[75]["time"],
                   "start_price":11.2,"end_price":10.8,"display_confirmed":False}]
    data["zones"]=[{"start_time":bars[10]["time"],"end_time":bars[30]["time"],"low":10,"high":11,
                    "direction":"up","display_confirmed":True},
                   {"start_time":bars[40]["time"],"end_time":bars[60]["time"],"low":10.5,"high":11.5,
                    "direction":"down","display_confirmed":True},
                   {"start_time":bars[70]["time"],"end_time":bars[75]["time"],"low":10.5,"high":11.5,
                    "direction":"up","display_confirmed":False}]
    expected=copy.deepcopy(data["prediction"])
    result=chart.render(data,tmp_path,dpi=72)
    figure=figures[-1]
    assert len(figure.axes)==3  # price, shared volume, thin quality bar in the hero
    price,volume,quality=figure.axes
    assert price.get_position().width>.65
    assert price.get_position().width==volume.get_position().width
    assert price.get_shared_x_axes().joined(price,volume)
    assert price.get_xlim()==volume.get_xlim()
    assert quality.get_position().y0>price.get_position().y1
    assert quality.get_position().height<.01
    assert price.yaxis.get_ticks_position()=="right"
    n=len(data["history"]["bars"])
    horizon=len(expected["forecast"])
    assert n-1 in volume.get_xticks() and n+horizon-1 in volume.get_xticks()
    assert n+1 not in volume.get_xticks()  # Avoid crowding the last observed and future dates.
    x=list(range(n-1,n+horizon))
    last=data["history"]["bars"][-1]["close"]
    median=next(line for line in price.lines if line.get_gid()=="forecast_median")
    np.testing.assert_array_equal(median.get_xdata(),x)
    np.testing.assert_array_equal(median.get_ydata(),[last,*[row["close"] for row in expected["forecast"]]])
    assert not any((line.get_gid() or "").startswith("forecast_sample_") for line in price.lines)
    for gid,key in (("forecast_p10","close_p10"),("forecast_p90","close_p90")):
        line=next(line for line in price.lines if line.get_gid()==gid)
        np.testing.assert_array_equal(line.get_xdata(),x)
        np.testing.assert_array_equal(line.get_ydata(),[last,*[row[key] for row in expected["forecast"]]])
    terminal_labels=[text for text in price.texts if (text.get_gid() or "").startswith("forecast_terminal_label_")]
    assert len(terminal_labels)==3
    assert all(text.get_ha()=="right" and text.get_position()[0]<1 for text in terminal_labels)
    assert all(text.get_transform()==price.get_yaxis_transform() for text in terminal_labels)
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
    from matplotlib.colors import to_rgba
    palette=chart.load_theme()["palette"]
    np.testing.assert_array_equal(figure.get_facecolor(),to_rgba(palette["paper"]))
    np.testing.assert_array_equal(price.get_facecolor(),to_rgba(palette["card"]))
    candles=[patch for patch in price.patches if patch.get_gid()=="historical_candle"]
    assert len(candles)==n
    for rectangle,bar in zip(candles,data["history"]["bars"]):
        expected_color=palette["up"] if bar["close"]>=bar["open"] else palette["down"]
        np.testing.assert_array_equal(rectangle.get_facecolor(),to_rgba(expected_color))
    workspace=next(artist for artist in figure.artists if artist.get_gid()=="chart_workspace")
    sidebar=next(artist for artist in figure.artists if artist.get_gid()=="evidence_sidebar")
    assert sidebar.get_x()>workspace.get_x()+workspace.get_width()
    assert sidebar.get_y()==workspace.get_y()
    assert sidebar.get_height()==workspace.get_height()
    for gid,linestyle in (("confirmed_pen","-"),("forming_pen","--")):
        line=next(line for line in price.lines if line.get_gid()==gid)
        assert line.get_color()==palette["accent"]
        assert line.get_linestyle()==linestyle
    zones=[patch for patch in price.patches if patch.get_gid()=="confirmed_zone"]
    assert len(zones)==2
    for zone,key,source_zone in zip(zones,("zone_up","zone_down"),data["zones"]):
        np.testing.assert_array_equal(zone.get_facecolor(),to_rgba(palette[key],.09))
        np.testing.assert_array_equal(zone.get_edgecolor(),to_rgba(palette[key],.65))
        assert zone.get_linestyle()=="-"
        assert zone.get_y()==source_zone["low"]
        assert zone.get_height()==source_zone["high"]-source_zone["low"]
    assert not any("02  Prediction quality" in text.get_text() for text in figure.texts)
