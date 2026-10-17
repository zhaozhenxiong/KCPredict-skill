#!/usr/bin/env python3
"""Render a reusable three-month KCPredict composite as PNG, SVG and evidence JSON."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import textwrap
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kcpredict_chart_data import prepare_chart_data
from kcpredict_theme import load_theme, theme_identity


def number(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


def read_json(path: Path | None) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path else None


def configure_font(font_manager, ft2font, explicit: Path | None) -> tuple[str, bool]:
    """Use an available CJK font; ship English labels when none is installed."""
    paths = [explicit] if explicit else []
    paths += [Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
              Path("/System/Library/Fonts/PingFang.ttc"),
              Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
              Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts/msyh.ttc",
              Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    preferred = ("Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                 "Arial Unicode MS", "SimHei", "Heiti SC", "WenQuanYi Zen Hei")
    paths += [Path(f.fname) for name in preferred for f in font_manager.fontManager.ttflist if f.name == name]
    if explicit and not explicit.is_file():
        raise ValueError(f"Font does not exist: {explicit}")
    for path in paths:
        if path and path.is_file():
            try:
                mapping = ft2font.FT2Font(str(path)).get_charmap()
                if not all(ord(c) in mapping for c in "结构量价预测"):
                    continue
                font_manager.fontManager.addfont(str(path))
                return font_manager.FontProperties(fname=str(path)).get_name(), True
            except (OSError, RuntimeError):
                continue
    return "DejaVu Sans", False


def render(data: dict, output_dir: Path, *, font_path: Path | None = None, dpi: int = 190) -> dict:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager, ft2font
        from matplotlib.lines import Line2D
        from matplotlib.patches import FancyBboxPatch, Patch, Rectangle
        from matplotlib.ticker import MaxNLocator
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for chart rendering. Use the chart Python to install the KCPredict [charts] extra, "
                           "or run: python -m pip install 'matplotlib>=3.9,<4'. No packages were downloaded.") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = {"png":output_dir/"composite.png", "svg":output_dir/"composite.svg", "evidence":output_dir/"chart-data.json"}
    for path in targets.values():
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite chart output: {path}")
    family, chinese = configure_font(font_manager, ft2font, font_path)
    theme = load_theme()
    identity = theme_identity(theme)
    COLORS = theme["palette"]
    typography = theme["typography"]
    layout = theme["layout"]
    available_weights=[entry.weight for entry in font_manager.fontManager.ttflist if entry.name==family]
    font_weight=min(available_weights,key=lambda value:abs((value if isinstance(value,(int,float)) else
        font_manager.weight_dict.get(value,400))-400)) if available_weights else "normal"
    t = lambda zh, en: zh if chinese else en
    plt.rcParams.update({"font.family":family,"font.size":typography["body_px"]*.75,"axes.unicode_minus":False,
        "font.weight":font_weight,"axes.labelweight":font_weight,
        "svg.fonttype":"path","axes.edgecolor":COLORS["line"],"axes.labelcolor":COLORS["muted"],
        "text.color":COLORS["ink"],"xtick.color":COLORS["muted"],"ytick.color":COLORS["muted"]})
    meta, hist = data["meta"], data["history"]
    bars, full = hist["bars"], data["full_history"]
    if not bars:
        raise ValueError("No eligible closed daily bars to plot")
    n = len(bars)
    indexes = {b["time"][:10]:i for i,b in enumerate(bars)}
    first, latest = bars[0], bars[-1]
    last_close = latest["close"]
    change = last_close/first["close"]-1 if first["close"] else None
    structure, pv, ict, prediction = data["structure"], data["price_volume"], data["ict"], data["prediction"]
    sc = structure.get("current_state") or {}
    wc = pv.get("current_state") or {}
    w32 = pv.get("w32_latest") or {}
    quality = prediction.get("quality") or {}
    pstatus = prediction["status"]
    ptext = {"pass":t("预测质量通过","QUALITY PASSED"),"fail":t("预测质量未通过","QUALITY FAILED"),
             "unavailable":t("预测不可用","FORECAST UNAVAILABLE"),"not_executed":t("预测未执行","FORECAST NOT RUN")}[pstatus]
    pcolor = COLORS["down"] if pstatus == "pass" else COLORS["amber"] if pstatus == "fail" else COLORS["muted"]
    sdir = structure.get("direction_state") if structure.get("status") in ("available","partial") else None
    vdir = pv.get("direction_state") if pv.get("status") in ("available","partial") else None
    direction = lambda x: {"BUY":t("偏多","bullish"),"SELL":t("偏空","bearish"),"NEUTRAL":t("中性","neutral")}.get(x,t("待核验","unverified"))
    if sdir in ("BUY","SELL") and vdir in ("BUY","SELL") and sdir != vdir:
        relationship = t("结构与量价方向有分歧","Structure and price-volume disagree")
    elif sdir in ("BUY","SELL") and sdir==vdir:
        relationship = t("结构与量价同向，ICT与上下文仍需核验","Structure and price-volume align; verify ICT/context")
    else:
        relationship = t("方向证据不足，保留可用观察与缺失原因","Insufficient directional evidence; retain observations and gaps")
    fvg = ict.get("latest_fvg") or ict.get("latest_bearish_fvg") or ict.get("latest_bullish_fvg")
    notes = ict.get("notes") or []
    note = next((x for x in reversed(notes) if x.get("type") in ("ssl_candidate","bsl_candidate")),None)

    fig = plt.figure(figsize=(layout["max_width_px"]/90,11.7),facecolor=COLORS["paper"])
    def rule(x0, x1, y, *, color=None, width=.8):
        fig.add_artist(Line2D([x0,x1],[y,y],transform=fig.transFigure,
            color=color or COLORS["line"],lw=width))
    fig.text(.055,.963,"KCPREDICT",fontsize=11.5,fontweight="semibold",color=COLORS["ink"])
    fig.text(.157,.964,"PRICE · STRUCTURE · SCENARIOS",fontsize=7.8,color=COLORS["muted"])
    fig.text(.945,.964,t("本地研究档案 · 单文件离线可用","LOCAL RESEARCH ARCHIVE · OFFLINE"),
        ha="right",fontsize=8.3,color=COLORS["muted"])
    rule(.055,.945,.944)
    fig.text(.055,.918,"MARKET STRUCTURE / CONTINUOUS TIMELINE",fontsize=7.8,color=COLORS["muted"])
    fig.text(.055,.876,meta["symbol"],fontsize=typography["title_px"]*.75,fontweight="semibold",color=COLORS["ink"])
    fig.text(.238,.884,t(f"近{meta['months']}个月综合研究",f"{meta['months']}-month composite research"),
        fontsize=12.5,color=COLORS["muted"])
    basis = t("未复权","unadjusted") if meta.get("price_basis")=="unadjusted" else t("价格口径未核验","unverified price basis")
    resource=prediction.get("resource") or {}
    device=resource.get("actual_device") or (t("未执行","not run") if pstatus=="not_executed" else t("设备未报告","device unreported"))
    fig.text(.055,.846,f"{hist['date_start'][:10]} — {hist['date_end'][:10]}  ·  {n} "
        +t(f"根闭合日线  ·  {basis}价格 / 元  ·  Asia/Shanghai",f"closed daily bars  ·  {basis} CNY  ·  Asia/Shanghai"),
        fontsize=9,color=COLORS["muted"])
    fig.text(.055,.797,number(last_close),fontsize=typography["quote_px"]*.75,color=COLORS["ink"])
    fig.text(.148,.807,t(f"窗口变化 {change:+.2%}",f"Window change {change:+.2%}"),
        fontsize=10,color=COLORS["up"] if change>=0 else COLORS["down"])
    fig.text(.325,.807,relationship,fontsize=9.5,color=COLORS["muted"])
    fig.add_artist(Line2D([.703,.703],[.795,.912],transform=fig.transFigure,color=pcolor,lw=1.8))
    fig.text(.720,.893,ptext,fontsize=11.7,fontweight="semibold",color=pcolor)
    description = (t("通过样本与输入可用性检查，仍需独立判断。","Sample and input checks passed; independent judgment is still required.") if pstatus=="pass" else
        t("仅供观察采样情景，不能视为可用预测。","Sample scenarios only; this is not a usable forecast.") if pstatus=="fail" else
        prediction.get("reason") or t("保留已有历史证据与缺失原因。","Retain available history and the reported missing inputs."))
    if not chinese and not description.isascii():
        description="Missing assets, inputs or execution; inspect chart-data.json."
    description_lines=textwrap.wrap(description,27 if chinese else 45)
    fig.text(.720,.869,"\n".join(description_lines[:2]),fontsize=8.1,color=COLORS["muted"],va="top",linespacing=1.6)
    valid, requested = quality.get("valid_samples"), quality.get("requested_samples")
    rate = quality.get("valid_fraction")
    forecast_rows = prediction.get("forecast") or []
    horizon = len(forecast_rows) or (prediction.get("parameters") or {}).get("horizon",5)
    fig.add_artist(FancyBboxPatch((.055,.300),.890,.470,boxstyle="round,pad=0,rounding_size=.008",
        transform=fig.transFigure,facecolor=COLORS["card"],edgecolor=COLORS["line"],lw=.7,zorder=-10,gid="chart_workspace"))
    fig.text(.071,.742,t("价格、结构与未来路径","PRICE, STRUCTURE AND SCENARIOS"),
        fontsize=11.5,fontweight="semibold",color=COLORS["ink"])
    fig.text(.285,.743,t(f"近{meta['months']}个月 · 连续时间轴",f"{meta['months']} months · continuous timeline"),
        fontsize=8.5,color=COLORS["muted"])
    ax = fig.add_axes([.071,.435,.838,.252],facecolor=COLORS["card"])
    vx = fig.add_axes([.071,.356,.838,.060],sharex=ax,facecolor=COLORS["card"])
    qx = fig.add_axes([.720,.827,.225,.004],facecolor=COLORS["paper"])
    for axis in (ax,vx):
        axis.spines[["top","left"]].set_visible(False)
        axis.spines[["bottom","right"]].set_color(COLORS["line"])
        axis.grid(axis="y",color=COLORS["grid"],lw=.65)
        axis.set_axisbelow(True)
        axis.yaxis.tick_right()
        axis.yaxis.set_label_position("right")
        axis.tick_params(length=0,pad=7,labelsize=typography["chart_px"]*.75)
    plt.setp(ax.get_xticklabels(),visible=False)
    price_values = [v for b in bars for v in (b["low"],b["high"])]
    price_values += [v for r in forecast_rows for v in (r.get("close_p10"),r.get("close_p90")) if isinstance(v,(int,float)) and math.isfinite(v)]
    for manual in notes:
        if isinstance(manual.get("mss_reference_price"),(int,float)):
            price_values.append(manual["mss_reference_price"])
    low, high = min(price_values),max(price_values)
    span = max(high-low,abs(last_close)*.05,.01)
    ymin,ymax = max(0,low-span*.12),high+span*.18
    ax.set_ylim(ymin,ymax)
    ax.set_xlim(-1,n+horizon+2.1)
    ax.yaxis.set_major_locator(MaxNLocator(6))
    ax.text(1.012,1.015,t("价格 / 元","Price / CNY"),transform=ax.transAxes,fontsize=7.9,color=COLORS["muted"])
    for axis in (ax,vx):
        axis.axvspan(n-.5,n+horizon-.5,facecolor=COLORS["forecast_top"],edgecolor="none",zorder=-1,gid="forecast_region")
        axis.axvline(n-.5,color=COLORS["cutoff"],ls=(0,(3,4)),lw=.8,alpha=.7,gid="prediction_boundary")
    ax.axhline(last_close,color=COLORS["close_reference"],ls=(0,(2,4)),lw=.6,alpha=.8)
    ax.text(n-.15,ymax-span*.025,t("未来情景","FORECAST"),fontsize=8.3,color=COLORS["blue"],va="top")
    for i,b in enumerate(bars):
        rising=b["close"]>=b["open"]
        candle_color=COLORS["up"] if rising else COLORS["down"]
        ax.vlines(i,b["low"],b["high"],color=candle_color,lw=.8,zorder=3)
        candle=Rectangle((i-.28,min(b["open"],b["close"])),.56,
            max(abs(b["close"]-b["open"]),span*.0015),facecolor=candle_color,
            edgecolor=candle_color,lw=.6,zorder=4,gid="historical_candle")
        ax.add_patch(candle)

    def xpos(day: Any, *, end=False) -> float:
        key=str(day)[:10]
        if key in indexes:
            return indexes[key]
        return n-1 if end or key > str(latest["time"])[:10] else 0

    visible_zones=[z for z in data.get("zones",[]) if z.get("display_confirmed") and str(z.get("end_time",""))[:10] >= str(first["time"])[:10]][-2:]
    for j,z in enumerate(visible_zones):
        color=COLORS["gold"] if j==len(visible_zones)-1 else COLORS["muted"]
        x0,x1=xpos(z["start_time"]),xpos(z["end_time"],end=True)
        ax.add_patch(Rectangle((x0,z["low"]),max(x1-x0,.6),z["high"]-z["low"],
            facecolor=COLORS["zone_fill"] if j==len(visible_zones)-1 else COLORS["zone_fill_muted"],
            edgecolor=COLORS["zone_edge"],lw=.7,ls=(0,(4,3)),alpha=.65,zorder=0))
        caption=f"{number(z['low'])}–{number(z['high'])}"
        when=str(z.get("confirmation_time") or z.get("confirmed_at") or "")[:10]
        caption+=" · "+t("确认 ","Known ")+(when or t("未核验","unverified"))
        ax.text(x0+.5,z["low"]+span*.017,caption,color=COLORS["chart_label"],fontsize=7.6,zorder=7)
    for pen in data.get("pens",[]):
        if str(pen.get("start_time",""))[:10] not in indexes or str(pen.get("end_time",""))[:10] not in indexes:
            continue
        confirmed=pen.get("display_confirmed") is True
        ax.plot([xpos(pen["start_time"]),xpos(pen["end_time"])],[pen["start_price"],pen["end_price"]],
            color=COLORS["gold"],lw=1.9 if confirmed else 1.2,ls="-" if confirmed else "--",alpha=.95,zorder=5)
    # Only eligible, observed source facts can mark the known confirmation day.
    facts=[f for f in structure.get("support_facts",[]) if str(f.get("first_confirmed_at") or f.get("confirmed_at") or "")[:10] == str(latest["time"])[:10]]
    labels=list(dict.fromkeys(str(f.get("label")) for f in facts if f.get("label") not in (None,"其他")))
    if labels:
        text=" / ".join(labels) if chinese else t("来源形态确认","Source pattern confirmed")
        ax.scatter([n-1],[last_close],marker="D",s=20,color=COLORS["gold"],zorder=10)
        ax.annotate(str(latest["time"])[5:10]+"  "+text,xy=(n-1,last_close),
            xytext=(-8,-16),textcoords="offset points",ha="right",fontsize=7.7,color=COLORS["gold"],zorder=9)
    ax.text(0,1.015,t("实测截止 ","Observed through ")+str(latest["time"])[:10],
        transform=ax.transAxes,fontsize=7.8,color=COLORS["muted"])

    if fvg and fvg.get("zone"):
        lower,upper=fvg["zone"]
        confirm=fvg.get("confirmed_time") or fvg.get("time")
        x0=xpos(confirm)
        ax.add_patch(Rectangle((x0-.25,lower),max(n-x0,.65),upper-lower,
            facecolor=COLORS["fvg"],edgecolor=COLORS["fvg"],lw=.7,alpha=.26,zorder=6))
        side=t("空头","Bearish") if fvg.get("direction")=="bearish" else t("多头","Bullish")
        lifecycle=(fvg.get("lifecycle") or {}).get("status")
        life_text={"active":t("尚未触及","untouched"),"touched":t("已触及","touched"),
                   "filled":t("已回补","filled"),"closed_through":t("已收盘穿越","closed through")}.get(lifecycle,"")
        caption=f"{side} FVG {number(lower)}–{number(upper)} · "+t("确认 ","Known ")+str(confirm)[5:10]
        ax.text(max(0,x0-.4),upper+span*.023,caption,
            ha="right" if x0>n*.6 else "left",color=COLORS["fvg"],fontsize=7.8,zorder=9)
    if note:
        sweep_x=xpos(note["date"])
        ref_x=xpos(note["reference_date"])
        extreme=note["extreme"]
        ax.plot([ref_x,sweep_x],[note["reference_price"]]*2,color=COLORS["purple"],ls=":",lw=1.1,zorder=6)
        ax.scatter([sweep_x],[extreme],facecolor=COLORS["card"],edgecolor=COLORS["purple"],s=24,zorder=9)
        tag="SSL" if note["type"]=="ssl_candidate" else "BSL"
        ax.annotate(f"{str(note['date'])[5:10]} {tag} "+t("候选","candidate")+f" {number(extreme)}",
            xy=(sweep_x,extreme),xytext=(-7,-13),textcoords="offset points",ha="right",
            color=COLORS["purple"],fontsize=7.8,zorder=9)
        mss=note.get("mss_reference_price")
        if isinstance(mss,(int,float)):
            ax.plot([sweep_x,n-1],[mss,mss],color=COLORS["purple"],ls="--",lw=1)
            ax.text(max(0,sweep_x-.5),mss+span*.02,t("MSS参考 ","MSS reference ")+number(mss),
                ha="right",fontsize=8,color=COLORS["purple"])

    legend_handles=[Line2D([0],[0],color=COLORS["gold"],lw=1.9,label=t("确认笔（回溯锚点）","Confirmed strokes")),
        Line2D([0],[0],color=COLORS["gold"],ls="--",lw=1.2,label=t("形成中笔","Forming stroke")),
        Patch(facecolor=COLORS["zone_fill"],edgecolor=COLORS["gold"],ls="--",label=t("回溯中枢","Retrospective zones")),
        Line2D([0],[0],color=COLORS["purple"],lw=1,label="ICT")]
    if forecast_rows:
        legend_handles += [Line2D([0],[0],color=COLORS["blue"],ls="--",lw=2,label=t("预测收盘中位","Forecast median close")),
                           Patch(facecolor=COLORS["band"],alpha=.25,label=t("P10–P90 样本分位","P10-P90 sample quantiles"))]
    ax.legend(handles=legend_handles,loc="lower left",bbox_to_anchor=(-.006,1.10),ncol=len(legend_handles),
        frameon=False,fontsize=8.3,columnspacing=1.6,handlelength=1.7)

    full_idx={str(b["time"])[:10]:i for i,b in enumerate(full)}
    volumes=[b.get("volume")/1e8 if isinstance(b.get("volume"),(int,float)) else np.nan for b in bars]
    averages=[]
    for b in bars:
        i=full_idx[str(b["time"])[:10]]
        prior=full[max(0,i-20):i]
        values=[r.get("volume") for r in prior]
        averages.append(float(np.mean(values))/1e8 if len(values)==20 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in values) else np.nan)
    vx.bar(range(n),volumes,width=.68,color=[COLORS["up"] if b["close"]>=b["open"] else COLORS["down"] for b in bars],alpha=.55,zorder=2)
    vx.plot(range(n),averages,color=COLORS["gold"],lw=1.1)
    forecast_volumes=[r.get("volume")/1e8 if isinstance(r.get("volume"),(int,float)) else np.nan for r in forecast_rows]
    if forecast_volumes:
        future_volume=vx.bar(range(n,n+len(forecast_volumes)),forecast_volumes,width=.68,
            color=COLORS["volume_future"],edgecolor="none",alpha=.65,zorder=2)
        for rectangle in future_volume:
            rectangle.set_gid("forecast_volume")
    finite_vol=[v for v in volumes+averages if math.isfinite(v)]
    finite_vol += [v for v in forecast_volumes if math.isfinite(v)]
    vx.set_ylim(0,max(finite_vol)*1.35 if finite_vol and max(finite_vol)>0 else 1)
    vx.text(1.012,.98,t("量 / 亿股","100m shares"),transform=vx.transAxes,fontsize=7.5,color=COLORS["muted"],va="top")
    count=min(7,n)
    ticks=sorted(set(np.round(np.linspace(0,n-1,count)).astype(int).tolist()))
    tick_labels=[str(bars[i]["time"])[5:10].replace("-","/") for i in ticks]
    if forecast_rows:
        future_ticks=([1,len(forecast_rows)-1] if len(forecast_rows)>=4 else [len(forecast_rows)-1])
        for index in sorted(set(future_ticks)):
            ticks.append(n+index)
            tick_labels.append(str(forecast_rows[index]["date"])[5:10].replace("-","/"))
    vx.set_xticks(ticks,tick_labels,fontsize=7.8)
    rule(.055,.945,.326,color=COLORS["foot_line"])
    fig.text(.071,.309,t("收涨 / 收跌  ·  金色：结构与此前20日均量  ·  紫色：ICT  ·  蓝色：预测情景",
        "Up / down candles · gold: structure and prior 20-bar volume · purple: ICT · blue: scenarios"),
        fontsize=7.8,color=COLORS["muted"])
    fig.text(.930,.309,t("P10–P90 为有效样本分位区间","P10-P90: valid-sample quantiles"),
        ha="right",fontsize=7.8,color=COLORS["muted"])

    if forecast_rows:
        horizon=len(forecast_rows)
        x=np.arange(n-1,n+horizon)
        # Match the HTML's default layers: raw samples remain in evidence only.
        low_values=[last_close,*[r["close_p10"] for r in forecast_rows]]
        high_values=[last_close,*[r["close_p90"] for r in forecast_rows]]
        median_values=[last_close,*[r["close"] for r in forecast_rows]]
        ax.fill_between(x,low_values,high_values,color=COLORS["band"],alpha=.25,zorder=2,gid="forecast_quantile_band")
        ax.plot(x,low_values,color=COLORS["band_edge"],lw=.65,zorder=2,gid="forecast_p10")
        ax.plot(x,high_values,color=COLORS["band_edge"],lw=.65,zorder=2,gid="forecast_p90")
        ax.plot(x,median_values,color=COLORS["blue"],ls=(0,(6,4)),lw=1.9,marker="o",ms=3.8,mfc=COLORS["card"],
            zorder=5,gid="forecast_median")
        for key,v,label in [("p90",high_values[-1],"P90"),("median",median_values[-1],t("中位","Median")),
                            ("p10",low_values[-1],"P10")]:
            ax.text(.988,v,f"{label} {number(v)}",transform=ax.get_yaxis_transform(),ha="right",
                fontsize=8.4,color=COLORS["blue"],va="center",gid=f"forecast_terminal_label_{key}",zorder=10,
                bbox={"fc":COLORS["card"],"ec":"none","alpha":.84,"pad":1.1})
        for v in (sc.get("support_floor"),sc.get("resistance_ceiling")):
            if isinstance(v,(int,float)):
                ax.plot([n-1,n+horizon-.5],[v,v],color=COLORS["gold"],ls=":",lw=1)
        if fvg and fvg.get("zone") and (fvg.get("lifecycle") or {}).get("status") not in ("filled","closed_through"):
            ax.add_patch(Rectangle((n-.5,fvg["zone"][0]),horizon,fvg["zone"][1]-fvg["zone"][0],
                facecolor=COLORS["fvg"],edgecolor="none",alpha=.2,zorder=3))
    else:
        empty_label={"unavailable":t("预测\n不可用","FORECAST\nUNAVAILABLE"),
                     "not_executed":t("预测\n未执行","FORECAST\nNOT RUN")}.get(pstatus,ptext)
        ax.text(n-.05,ymin+span*.55,empty_label,color=pcolor,fontsize=10,va="center")

    if isinstance(rate,(int,float)):
        threshold=quality.get("minimum_valid_fraction")
        qx.barh([0],[100],height=.72,color=COLORS["quality_track"])
        qx.barh([0],[min(1,max(0,rate))*100],height=.72,color=pcolor)
        if isinstance(threshold,(int,float)):
            qx.axvline(threshold*100,color=COLORS["quality_threshold"],ls=(0,(2,2)),lw=.75)
        qx.set_xlim(0,100);qx.set_ylim(-.4,.4);qx.set_yticks([])
        qx.set_xticks([])
        fig.text(.720,.804,t(f"有效 {valid}/{requested} · {rate:.3%} / 门槛 {number(threshold*100 if threshold is not None else None,0)}% · 最少{number(quality.get('minimum_valid_samples'),0)}条",
            f"Valid {valid}/{requested} · {rate:.3%} / {number(threshold*100 if threshold is not None else None,0)}% · minimum {number(quality.get('minimum_valid_samples'),0)}"),
            fontsize=7.7,color=COLORS["muted"])
    else:
        qx.set_xticks([])
        fig.text(.720,.804,t("未提供可评估的有效样本统计","No evaluable valid-sample statistics reported"),
            fontsize=7.9,color=COLORS["muted"])
    qx.set_frame_on(False);qx.set_yticks([])

    def wrapped_lines(text: str, width: int = 35) -> list[str]:
        # CJK glyphs need more horizontal room than ASCII digits/labels.
        lines=[]; current=""; size=0
        for ch in text:
            step=2 if ord(ch)>255 else 1
            if size+step>width*2:
                lines.append(current);current="";size=0
            current+=ch;size+=step
        if current:lines.append(current)
        return lines

    structure_lines=[t("当前支撑 ","Support ")+number(sc.get("support_floor"))+t(" / 上沿 "," / upper ")+number(sc.get("resistance_ceiling"))]
    if visible_zones:
        z=visible_zones[-1]
        structure_lines.append(t("中枢确认 ","Zone known ")+str(z.get("confirmation_time") or z.get("confirmed_at") or "unverified")[:10])
    if labels:
        structure_lines.append(t("标签 ","Source labels ")+(" / ".join(labels) if chinese else "pattern labels")+t("；同源不重复计票","; correlated labels"))
    structure_lines.append(t("锚点与确认日分开，形成中笔不作确认。","Anchors differ from confirmation; forming strokes remain provisional."))
    rv=pv.get("relative_volume20")
    close_location=w32.get("w_close_location")
    pv_lines=[t("相对前20日量 ","Relative volume ")+number(rv)+t("倍","x")]
    if isinstance(close_location,(int,float)):
        pv_lines.append(t("收盘位置（当日低→高） ","Close position (daily low to high) ")+f"{close_location:.1%}")
    event=w32.get("wyckoff_event")
    pv_lines.append(t("最新事件 ","Latest event ")+(t("无新事件","none") if event=="none" else str(event if event is not None else "unverified")))
    if wc.get("buy_score") is not None and wc.get("sell_score") is not None:
        pv_lines.append(t("需求 / 供给 ","Demand / supply ")+f"{number(wc['buy_score'],3)} / {number(wc['sell_score'],3)}")
    pv_lines.append(t("旧事件状态延续不等于新需求确认。","Legacy event state is not a fresh demand confirmation."))
    candidate=ict.get("latest_candidate") or {}
    ict_lines=[]
    if note:
        tag="SSL" if note["type"]=="ssl_candidate" else "BSL"
        ict_lines.append(str(note["date"])[5:10]+f" {tag} "+t("人工候选 ","reviewed candidate ")+number(note["extreme"]))
        ict_lines.append(t("旧点 ","Reference ")+number(note["reference_price"])+t(" / 收盘 "," / close ")+number(note["close"]))
        if note.get("mss_reference_price") is not None:
            ict_lines.append(t("原设定MSS参考 ","Original MSS reference ")+number(note["mss_reference_price"]))
    else:
        ict_lines.append(t("机械候选 ","Mechanical candidate ")+str(candidate.get("mechanical_status") or "unavailable"))
        if candidate.get("sweep"):
            ict_lines.append(str(candidate["sweep"].get("time",""))[:10]+" / "+str(candidate.get("direction","")))
    if fvg:
        ict_lines.append(t("最新严格FVG ","Latest strict FVG ")+f"{number(fvg['zone'][0])}–{number(fvg['zone'][1])}")
    pending=[]
    if note:
        matching="bullish" if note["type"]=="ssl_candidate" else "bearish"
        gaps=[g for g in ict.get("recent_fvgs",[]) if g.get("direction")==matching
              and str(g.get("confirmed_time") or g.get("time"))[:10]>str(note["date"])[:10]]
        if not gaps:
            pending.append("FVG")
        mss=note.get("mss_reference_price")
        if isinstance(mss,(int,float)):
            crossed=any((b["close"]>mss if matching=="bullish" else b["close"]<mss)
                        for b in full if str(b["time"])[:10]>str(note["date"])[:10])
            if not crossed:
                pending.append("MSS")
        if pending:
            pending.append(t("有效回撤","valid retrace"))
    else:
        checks=candidate.get("checks") or {}
        for key,label in (("displacement_and_strict_fvg","FVG"),("closed_candle_mss","MSS"),("retrace_observed",t("有效回撤","retrace"))):
            if checks.get(key) is False:
                pending.append(label)
    ict_lines.append((t("待确认：","Pending: ")+" / ".join(pending)) if pending else
                     t("位移、结构、回撤与净R:R分别核验。","Verify displacement, structure, retest and net R:R."))
    ict_lines.append(t("机械状态不等于最终交易确认。","Mechanical status does not establish a final setup."))

    fig.text(.055,.270,t("证据与共振","EVIDENCE AND CONFLUENCE"),fontsize=10.5,fontweight="semibold",color=COLORS["ink"])
    rule(.055,.945,.254,color=COLORS["divider"])
    rule(.055,.125,.254,color=COLORS["ink"],width=1.4)
    for x in (.347,.660):
        fig.add_artist(Line2D([x,x],[.104,.235],transform=fig.transFigure,color=COLORS["evidence_line"],lw=.7))
    final_label={"not_assessed":t("最终确认尚待核验","Final context unverified"),
                 "confirmed":t("最终设定已确认","Final setup confirmed"),
                 "developing":t("设定形成中","Setup developing"),
                 "invalidated":t("原设定已失效","Setup invalidated"),
                 "no-trade":t("暂无可执行设定","No executable setup")}.get(ict.get("final_status"),t("最终确认尚待核验","Final context unverified"))
    available=lambda value: {"available":t("输入可用","Input available"),"partial":t("部分可用","Partially available"),
        "unavailable":t("暂不可用","Unavailable"),"completed":t("已扫描","Scanned")}.get(value,t("待核验","Unverified"))
    panels=[(.055,t("CZSC 结构","CZSC STRUCTURE"),available(structure.get("status")),direction(sdir),structure_lines),
            (.368,t("威科夫 / 量价","WYCKOFF / PRICE-VOLUME"),available(pv.get("status")),direction(vdir),pv_lines),
            (.681,t("ICT / SMC 验证","ICT / SMC VALIDATION"),available(ict.get("status")),final_label,ict_lines)]
    for x,title,status_label,bias,lines in panels:
        fig.text(x,.233,title,fontsize=9.9,fontweight="semibold",color=COLORS["ink"])
        fig.text(x,.208,bias,fontsize=13.5,color=COLORS["ink"])
        fig.text(x+.195,.233,status_label,ha="right",fontsize=7.6,color=COLORS["muted"])
        expanded=[s for line in lines for s in wrapped_lines(line)]
        for y,line in zip([.187,.17,.153,.136,.119,.102],expanded[:6]):
            fig.text(x,y,line,fontsize=8.7,color=COLORS["evidence_ink"])
    rule(.055,.945,.090,color=COLORS["divider"])
    takeaway=relationship+" · "+(t("预测质量未通过，路径只作情景参考。","Prediction quality failed; paths are sample scenarios only.") if pstatus=="fail" else
        t("预测可用性通过，不能替代结构、量价和ICT的独立验证。","Prediction availability passed; verify structure, volume and ICT independently.") if pstatus=="pass" else ptext)
    fig.text(.055,.075,takeaway,fontsize=9.2,color=COLORS["amber"])
    fig.text(.055,.049,t("时钟：笔与中枢为当前截止的回溯几何；锚点与确认时间分开；形成桶和未来情景不作历史确认。P10–P90并非校准置信区间。",
        "Timing: retrospective anchors differ from confirmation. Forming bars and forecast scenarios cannot confirm history. Quantiles are uncalibrated."),
        fontsize=8,color=COLORS["fineprint"])
    provenance=t("本地用户声明数据；未独立认证行情、公司行为与成交可行性。","Local user-declared data; prices, corporate actions and execution are not independently certified.")
    fig.text(.055,.031,provenance,fontsize=8,color=COLORS["fineprint"])
    if not chinese:
        data.setdefault("render_warnings",[]).append("No CJK font available; English chart labels were used.")
    data["render"]={"style":"kcpredict-composite-v4","font":family,"language":"zh" if chinese else "en","dpi":dpi,
                    "theme":identity,"visual_reference":"assets/composite.html",
                    "layer_defaults":{"structure":True,"ict":True,"prediction":True,"band":True,"samples":False,"volume":True},
                    "layout":"single_continuous_timeline",
                    "timeline":{"historical_x":[0,n-1],"forecast_anchor_x":n-1,
                                "forecast_x":list(range(n,n+len(forecast_rows))),"volume_axis_shared":True},
                    "files":{key:path.name for key,path in targets.items()}}
    try:
        for kind in ("png","svg"):
            with targets[kind].open("xb") as stream:
                fig.savefig(stream,format=kind,dpi=dpi,facecolor=fig.get_facecolor())
        with targets["evidence"].open("x",encoding="utf-8") as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.write("\n")
    finally:
        plt.close(fig)
    return {"schema":"kcpredict_chart_artifacts_v1","symbol":meta["symbol"],"data_end":meta["data_end"],
            "prediction_status":pstatus,"prediction_usable":pstatus=="pass","theme":identity,
            **{key:str(path.resolve()) for key,path in targets.items()}}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,required=True,help="Research or structure JSON with real daily history")
    parser.add_argument("--prediction",type=Path,help="Optional forecast research JSON or explicit unavailable/not-executed record")
    parser.add_argument("--ict",type=Path,help="Optional same-source daily ICT JSON; otherwise scan historical bars")
    parser.add_argument("--notes",type=Path,help="Optional source-checked manual annotation JSON")
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--months",type=int,default=3)
    parser.add_argument("--font",type=Path,help="Optional local CJK font; never downloaded automatically")
    parser.add_argument("--dpi",type=int,default=190)
    args=parser.parse_args(argv)
    try:
        if not 72<=args.dpi<=400:
            raise ValueError("dpi must be between 72 and 400")
        prepared=prepare_chart_data(read_json(args.input),prediction_payload=read_json(args.prediction),
            ict_payload=read_json(args.ict),notes=read_json(args.notes),months=args.months)
        prepared["source_files"]={str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (args.input,args.prediction,args.ict,args.notes) if path}
        result=render(prepared,args.output_dir,font_path=args.font,dpi=args.dpi)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (OSError,ValueError,TypeError,KeyError,RuntimeError) as exc:
        print(json.dumps({"error":type(exc).__name__,"message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__=="__main__":
    raise SystemExit(main())
