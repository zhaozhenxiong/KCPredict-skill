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


COLORS = {"ink":"#183247", "muted":"#64798B", "blue":"#2768BB", "gold":"#A88739",
          "amber":"#AC651F", "purple":"#75619C", "gray":"#A4AFBA", "paper":"#F5F7FA",
          "grid":"#E7EDF2", "green":"#367160"}


def number(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


def read_json(path: Path | None) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path else None


def configure_font(font_manager, ft2font, explicit: Path | None) -> tuple[str, bool]:
    """Use an available CJK font; ship English labels when none is installed."""
    paths = [explicit] if explicit else []
    paths += [Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
              Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
              Path("/System/Library/Fonts/PingFang.ttc"),
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
    t = lambda zh, en: zh if chinese else en
    plt.rcParams.update({"font.family":family,"font.size":11.5,"axes.unicode_minus":False,
        "svg.fonttype":"path","axes.edgecolor":"#C6D0DA","axes.labelcolor":COLORS["muted"],
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
    pcolor = COLORS["green"] if pstatus == "pass" else COLORS["amber"] if pstatus == "fail" else COLORS["muted"]
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

    fig = plt.figure(figsize=(17.2,11.7),facecolor=COLORS["paper"])
    fig.add_artist(FancyBboxPatch((.024,.026),.952,.948,boxstyle="round,pad=.008,rounding_size=.014",
        transform=fig.transFigure,facecolor="white",edgecolor="none",zorder=-10))
    fig.text(.058,.942,"KCPREDICT  /  MARKET STRUCTURE RESEARCH",fontsize=10,color=COLORS["muted"])
    fig.text(.058,.901,meta["symbol"],fontsize=28,color=COLORS["ink"])
    fig.text(.222,.907,t(f"近{meta['months']}个月综合分析",f"{meta['months']}-month composite analysis"),fontsize=17)
    basis = t("未复权","unadjusted") if meta.get("price_basis")=="unadjusted" else t("价格口径未核验","unverified price basis")
    resource=prediction.get("resource") or {}
    device=resource.get("actual_device") or (t("未执行","not run") if pstatus=="not_executed" else t("设备未报告","device unreported"))
    fig.text(.058,.872,f"{hist['date_start'][:10]} — {hist['date_end'][:10]}  ·  {n} "
        +t(f"根闭合日线  ·  {basis}价格 / 元  ·  {device}",f"closed daily bars  ·  {basis} CNY prices  ·  {device}"),fontsize=11,color=COLORS["muted"])
    fig.text(.941,.921,ptext,ha="right",fontsize=14,color=pcolor,
        bbox={"boxstyle":"round,pad=.55,rounding_size=.17","facecolor":"#EFF5F2" if pstatus=="pass" else "#FFF4E8" if pstatus=="fail" else "#EFF2F5","edgecolor":"none"})
    valid, requested = quality.get("valid_samples"), quality.get("requested_samples")
    rate = quality.get("valid_fraction")
    if requested is not None and rate is not None:
        fig.text(.941,.88,t(f"有效 {valid}/{requested}  ·  有效率 {rate:.3%}",f"Valid {valid}/{requested}  ·  {rate:.3%}"),
                 ha="right",fontsize=11,color=COLORS["muted"])
    fig.text(.058,.831,t(f"收盘 {number(last_close)}  ·  窗口变化 {change:+.2%}  ·  {relationship}",
        f"Close {number(last_close)}  ·  Window change {change:+.2%}  ·  {relationship}"),fontsize=12.2)

    forecast_rows = prediction.get("forecast") or []
    horizon = len(forecast_rows) or (prediction.get("parameters") or {}).get("horizon",5)
    ax = fig.add_axes([.067,.448,.875,.319],facecolor="white")
    vx = fig.add_axes([.067,.322,.875,.075],sharex=ax,facecolor="white")
    qx = fig.add_axes([.744,.281,.198,.012],facecolor="white")
    ax.set_title(t(f"01  历史结构与未来{horizon}日情景 · 连续时间轴",
        f"01  Historical structure and next {horizon} trading days · one continuous timeline"),
        loc="left",fontsize=13,pad=17)
    for axis in (ax,vx):
        axis.spines[["top","right"]].set_visible(False)
        axis.grid(axis="y",color=COLORS["grid"],lw=.8)
        axis.set_axisbelow(True)
        axis.tick_params(length=0,pad=7,labelsize=10)
    plt.setp(ax.get_xticklabels(),visible=False)
    price_values = [v for b in bars for v in (b["low"],b["high"])]
    price_values += [v for r in forecast_rows for v in (r.get("close_p10"),r.get("close_p90")) if isinstance(v,(int,float)) and math.isfinite(v)]
    sample_paths = prediction.get("samples") or []
    if sample_paths:
        for path in sample_paths:
            price_values += [row[3] for row in path if isinstance(row[3],(int,float)) and math.isfinite(row[3])]
    for manual in notes:
        if isinstance(manual.get("mss_reference_price"),(int,float)):
            price_values.append(manual["mss_reference_price"])
    low, high = min(price_values),max(price_values)
    span = max(high-low,abs(last_close)*.05,.01)
    ymin,ymax = max(0,low-span*.12),high+span*.18
    ax.set_ylim(ymin,ymax)
    ax.set_xlim(-1,n+horizon+2.1)
    ax.yaxis.set_major_locator(MaxNLocator(6))
    ax.set_ylabel(t("价格 / 元","Price / CNY"),fontsize=10)
    for axis in (ax,vx):
        axis.axvspan(n-.5,n+horizon-.5,facecolor="#EDF5FF",edgecolor="none",zorder=-1,gid="forecast_region")
        axis.axvline(n-.5,color=COLORS["blue"],ls=(0,(4,4)),lw=1,alpha=.55,gid="prediction_boundary")
    ax.axvline(n-1,color=COLORS["gold"],ls=(0,(2,3)),lw=.7,alpha=.55)
    ax.text(n-.15,ymax-span*.025,t("预测情景","FORECAST"),fontsize=9.5,color=COLORS["blue"],va="top")
    for i,b in enumerate(bars):
        rising=b["close"]>=b["open"]
        ax.vlines(i,b["low"],b["high"],color=COLORS["ink"],lw=.8,zorder=3)
        ax.add_patch(Rectangle((i-.28,min(b["open"],b["close"])),.56,
            max(abs(b["close"]-b["open"]),span*.0015),facecolor=COLORS["ink"] if rising else "white",
            edgecolor=COLORS["ink"],lw=.9,zorder=4))

    def xpos(day: Any, *, end=False) -> float:
        key=str(day)[:10]
        if key in indexes:
            return indexes[key]
        return n-1 if end or key > str(latest["time"])[:10] else 0

    visible_zones=[z for z in data.get("zones",[]) if z.get("display_confirmed") and str(z.get("end_time",""))[:10] >= str(first["time"])[:10]][-2:]
    for j,z in enumerate(visible_zones):
        color=COLORS["gold"] if j==len(visible_zones)-1 else COLORS["gray"]
        x0,x1=xpos(z["start_time"]),xpos(z["end_time"],end=True)
        ax.add_patch(Rectangle((x0,z["low"]),max(x1-x0,.6),z["high"]-z["low"],
            facecolor="#F2EAD5" if color==COLORS["gold"] else "#EAF0F4",edgecolor=color,
            lw=.9,ls="--",alpha=.42,zorder=0))
        caption=t("回溯中枢","Retrospective zone")+f" {number(z['low'])}–{number(z['high'])}"
        when=str(z.get("confirmation_time") or z.get("confirmed_at") or "")[:10]
        caption+="\n"+t("确认 ","Known ")+(when or t("未核验","unverified"))
        ax.text(x0+.5,z["low"]+(z["high"]-z["low"])*.1,caption,color=color,fontsize=9.1,
            bbox={"fc":"white","ec":"none","alpha":.9,"pad":2},zorder=7)
    for pen in data.get("pens",[]):
        if str(pen.get("start_time",""))[:10] not in indexes or str(pen.get("end_time",""))[:10] not in indexes:
            continue
        confirmed=pen.get("display_confirmed") is True
        ax.plot([xpos(pen["start_time"]),xpos(pen["end_time"])],[pen["start_price"],pen["end_price"]],
            color=COLORS["gold"],lw=1.9 if confirmed else 1.2,ls="-" if confirmed else "--",alpha=.95,zorder=5)
    for tag,b in [(t("区间低","Window low"),min(bars,key=lambda b:b["low"])),
                  (t("区间高","Window high"),max(bars,key=lambda b:b["high"]))]:
        value=b["low"] if "低" in tag or tag=="Window low" else b["high"]
        offset=-span*.07 if value==low else span*.075
        ax.annotate(f"{str(b['time'])[5:10]}  {number(value)}",xy=(xpos(b["time"]),value),
            xytext=(xpos(b["time"])+.5,value+offset),fontsize=9.3,color=COLORS["muted"],
            arrowprops={"arrowstyle":"-","lw":.8,"color":COLORS["gray"]},zorder=8)

    # Only eligible, observed source facts can mark the known confirmation day.
    facts=[f for f in structure.get("support_facts",[]) if str(f.get("first_confirmed_at") or f.get("confirmed_at") or "")[:10] == str(latest["time"])[:10]]
    labels=list(dict.fromkeys(str(f.get("label")) for f in facts if f.get("label") not in (None,"其他")))
    if labels:
        text=" / ".join(labels) if chinese else t("来源形态确认","Source pattern confirmed")
        ax.scatter([n-1],[last_close],marker="D",s=28,color=COLORS["gold"],zorder=10)
        ax.annotate(str(latest["time"])[5:10]+"  "+text,xy=(n-1,last_close),
            xytext=(max(0,n*.57),ymin+span*.07),fontsize=9.7,color=COLORS["gold"],
            arrowprops={"arrowstyle":"-","color":COLORS["gray"],"lw":.8},
            bbox={"fc":"white","ec":"none","alpha":.94,"pad":3},zorder=9)
    ax.annotate(t("最后收盘 ","Last close ")+number(last_close),xy=(n-1,last_close),
        xytext=(-6,-17),textcoords="offset points",ha="right",fontsize=9.1,color=COLORS["ink"],
        bbox={"fc":"white","ec":"none","alpha":.9,"pad":2},zorder=10)

    if fvg and fvg.get("zone"):
        lower,upper=fvg["zone"]
        confirm=fvg.get("confirmed_time") or fvg.get("time")
        x0=xpos(confirm)
        ax.add_patch(Rectangle((x0-.25,lower),max(n-x0,.65),upper-lower,
            facecolor=COLORS["amber"],edgecolor=COLORS["amber"],alpha=.4,zorder=6))
        side=t("空头","Bearish") if fvg.get("direction")=="bearish" else t("多头","Bullish")
        lifecycle=(fvg.get("lifecycle") or {}).get("status")
        life_text={"active":t("尚未触及","untouched"),"touched":t("已触及","touched"),
                   "filled":t("已回补","filled"),"closed_through":t("已收盘穿越","closed through")}.get(lifecycle,"")
        caption=f"{side} FVG {number(lower)}–{number(upper)}\n"+t("确认 ","Known ")+str(confirm)[:10]+(" · "+life_text if life_text else "")
        ax.annotate(caption,xy=(x0,(lower+upper)/2),xytext=(max(0,n*.58),ymin+span*.59),
            color=COLORS["amber"],fontsize=9.8,
            arrowprops={"arrowstyle":"-","lw":.8,"color":COLORS["gray"]},
            bbox={"fc":"white","ec":"none","alpha":.94,"pad":3},zorder=9)
    if note:
        sweep_x=xpos(note["date"])
        ref_x=xpos(note["reference_date"])
        extreme=note["extreme"]
        ax.plot([ref_x,sweep_x],[note["reference_price"]]*2,color=COLORS["purple"],ls=":",lw=1.1,zorder=6)
        ax.scatter([sweep_x],[extreme],facecolor="white",edgecolor=COLORS["purple"],s=30,zorder=9)
        tag="SSL" if note["type"]=="ssl_candidate" else "BSL"
        ax.annotate(f"{str(note['date'])[5:10]} {tag} "+t("候选","candidate")+f" {number(extreme)}",
            xy=(sweep_x,extreme),xytext=(max(0,n*.55),ymin+span*.2),color=COLORS["purple"],fontsize=9.6,
            arrowprops={"arrowstyle":"-","lw":.8,"color":COLORS["gray"]},
            bbox={"fc":"white","ec":"none","alpha":.94,"pad":3},zorder=9)
        mss=note.get("mss_reference_price")
        if isinstance(mss,(int,float)):
            ax.plot([sweep_x,n-1],[mss,mss],color=COLORS["purple"],ls="--",lw=1)
            ax.text(max(0,n*.61),ymin+span*.96,t("MSS参考 ","MSS reference ")+number(mss),fontsize=10,color=COLORS["purple"])

    legend_handles=[Line2D([0],[0],color=COLORS["gold"],lw=1.9,label=t("确认笔（回溯锚点）","Confirmed strokes")),
        Line2D([0],[0],color=COLORS["gold"],ls="--",lw=1.2,label=t("形成中笔","Forming stroke")),
        Patch(facecolor="#F2EAD5",edgecolor=COLORS["gold"],ls="--",label=t("回溯中枢","Retrospective zones"))]
    if forecast_rows:
        legend_handles += [Line2D([0],[0],color=COLORS["blue"],ls="--",lw=2,label=t("预测收盘中位","Forecast median close")),
                           Patch(facecolor=COLORS["blue"],alpha=.14,label=t("P10–P90 样本分位","P10-P90 sample quantiles"))]
    ax.legend(handles=legend_handles,loc="lower left",bbox_to_anchor=(-.01,-.13),ncol=len(legend_handles),
        frameon=False,fontsize=9.2,columnspacing=1.4)

    full_idx={str(b["time"])[:10]:i for i,b in enumerate(full)}
    volumes=[b.get("volume")/1e8 if isinstance(b.get("volume"),(int,float)) else np.nan for b in bars]
    averages=[]
    for b in bars:
        i=full_idx[str(b["time"])[:10]]
        prior=full[max(0,i-20):i]
        values=[r.get("volume") for r in prior]
        averages.append(float(np.mean(values))/1e8 if len(values)==20 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in values) else np.nan)
    vx.bar(range(n),volumes,width=.68,color=[COLORS["ink"] if b["close"]>=b["open"] else "#B7C3CD" for b in bars],zorder=2)
    vx.plot(range(n),averages,color=COLORS["gold"],lw=1.5)
    forecast_volumes=[r.get("volume")/1e8 if isinstance(r.get("volume"),(int,float)) else np.nan for r in forecast_rows]
    if forecast_volumes:
        future_volume=vx.bar(range(n,n+len(forecast_volumes)),forecast_volumes,width=.68,
            color="#D5E6FA",edgecolor=COLORS["blue"],lw=.7,hatch="//",zorder=2)
        for rectangle in future_volume:
            rectangle.set_gid("forecast_volume")
    finite_vol=[v for v in volumes+averages if math.isfinite(v)]
    finite_vol += [v for v in forecast_volumes if math.isfinite(v)]
    vx.set_ylim(0,max(finite_vol)*1.35 if finite_vol and max(finite_vol)>0 else 1)
    vx.set_ylabel(t("成交量 / 亿股","Volume / 100m shares"),fontsize=9.5)
    count=min(7,n)
    ticks=sorted(set(np.round(np.linspace(0,n-1,count)).astype(int).tolist()))
    tick_labels=[str(bars[i]["time"])[5:10].replace("-","/") for i in ticks]
    if forecast_rows:
        future_ticks=([1,len(forecast_rows)-1] if len(forecast_rows)>=4 else [len(forecast_rows)-1])
        for index in sorted(set(future_ticks)):
            ticks.append(n+index)
            tick_labels.append(str(forecast_rows[index]["date"])[5:10].replace("-","/"))
    vx.set_xticks(ticks,tick_labels,fontsize=9.2)
    vx.text(.01,.92,t("实际量 / 金线：此前20日均量 / 蓝纹：预测中位量",
        "Actual volume / gold: previous 20-bar mean / blue hatch: forecast median"),
        transform=vx.transAxes,fontsize=9,color=COLORS["muted"])

    if forecast_rows:
        horizon=len(forecast_rows)
        x=np.arange(n-1,n+horizon)
        for index,path in enumerate(sample_paths):
            ax.plot(x,[last_close,*[row[3] for row in path]],color=COLORS["gray"],lw=.8,alpha=.2,
                zorder=1,gid=f"forecast_sample_{index}")
        low_values=[last_close,*[r["close_p10"] for r in forecast_rows]]
        high_values=[last_close,*[r["close_p90"] for r in forecast_rows]]
        median_values=[last_close,*[r["close"] for r in forecast_rows]]
        ax.fill_between(x,low_values,high_values,color=COLORS["blue"],alpha=.14,zorder=2,gid="forecast_quantile_band")
        ax.plot(x,median_values,color=COLORS["blue"],ls="--",lw=2.2,marker="o",ms=4,mfc="white",
            zorder=5,gid="forecast_median")
        for v,label in [(high_values[-1],"P90"),(median_values[-1],t("中位","Median")),(low_values[-1],"P10")]:
            ax.text(n+horizon-.6,v,f"{label} {number(v)}",fontsize=9,color=COLORS["blue"],va="center")
        for v in (sc.get("support_floor"),sc.get("resistance_ceiling")):
            if isinstance(v,(int,float)):
                ax.plot([n-1,n+horizon-.5],[v,v],color=COLORS["gold"],ls=":",lw=1)
        if fvg and fvg.get("zone") and (fvg.get("lifecycle") or {}).get("status") not in ("filled","closed_through"):
            ax.add_patch(Rectangle((n-.5,fvg["zone"][0]),horizon,fvg["zone"][1]-fvg["zone"][0],
                facecolor=COLORS["amber"],edgecolor="none",alpha=.2,zorder=3))
    else:
        empty_label={"unavailable":t("预测\n不可用","FORECAST\nUNAVAILABLE"),
                     "not_executed":t("预测\n未执行","FORECAST\nNOT RUN")}.get(pstatus,ptext)
        ax.text(n-.05,ymin+span*.55,empty_label,color=pcolor,fontsize=10,va="center")

    fig.text(.058,.278,t("02  预测质量检查","02  Prediction quality"),fontsize=11.7,color=pcolor)
    if isinstance(rate,(int,float)):
        threshold=quality.get("minimum_valid_fraction")
        qx.barh([0],[100],height=.72,color="#EDF1F5")
        qx.barh([0],[rate*100],height=.72,color=pcolor)
        if isinstance(threshold,(int,float)):
            qx.axvline(threshold*100,color=COLORS["ink"],ls="--",lw=1.1)
        qx.set_xlim(0,100);qx.set_ylim(-.4,.4);qx.set_yticks([])
        qx.set_xticks([0,50,100],["0%","50%","100%"],fontsize=8.2)
        qx.tick_params(length=0,pad=3)
        fig.text(.265,.278,t(f"有效 {valid}/{requested} · 有效率 {rate:.3%} · 要求 ≥{number(quality.get('minimum_valid_samples'),0)}条 / ≥{number(threshold*100 if threshold is not None else None,0)}%",
            f"Valid {valid}/{requested} · {rate:.3%} · minimum {number(quality.get('minimum_valid_samples'),0)} / {number(threshold*100 if threshold is not None else None,0)}%"),fontsize=9.8,color=COLORS["muted"])
        fail_reasons=[]
        minimum_count=quality.get("minimum_valid_samples")
        if isinstance(valid,int) and isinstance(minimum_count,int) and valid<minimum_count:
            fail_reasons.append(t(f"有效{valid}条 < {minimum_count}条",f"{valid} valid < {minimum_count} required"))
        if isinstance(threshold,(int,float)) and rate<threshold:
            fail_reasons.append(t(f"有效率{rate:.3%} < {threshold:.0%}",f"Valid fraction {rate:.3%} < {threshold:.0%}"))
        for key,zh,en in (("calendar_verified","日历门槛","calendar gate"),("input_contract_verified","输入契约","input contract"),("source_verified","来源门槛","source gate")):
            if quality.get(key) is False:
                fail_reasons.append(t(zh+"未通过",en+" failed"))
        fail=" / ".join(fail_reasons) or (t("原始质量标记未通过","Source quality flag failed") if pstatus=="fail" else "")
        if pstatus=="fail":
            fig.text(.058,.25,t("未通过：", "Failed: ")+fail[:125]
                +t("；图中仅保留采样情景。","; plotted paths are sample scenarios only."),fontsize=9.1,color=pcolor)
        else:
            fig.text(.058,.25,t("分位带来自有效样本，不是校准置信区间；预测情景不参与历史结构确认。",
                "Quantile bands use valid samples, not calibrated confidence intervals; forecasts cannot confirm history."),
                fontsize=9.1,color=COLORS["muted"])
    else:
        fig.text(.265,.278,ptext,color=pcolor,fontsize=10)
        reason=prediction.get("reason") or t("缺少可展示预测","No forecast available")
        if not chinese and not reason.isascii():
            reason="Missing assets, inputs or prediction execution; inspect chart-data.json."
        reason_lines=textwrap.wrap(reason,95 if chinese else 135)
        fig.text(.058,.25,(reason_lines[0]+("…" if len(reason_lines)>1 else "")) if reason_lines else "",
            fontsize=9.1,color=COLORS["muted"])
        qx.set_xticks([])
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

    structure_lines=[t("方向 ","Direction ")+str(sdir or "unverified")+t(" · 状态 "," · status ")+str(structure.get("status","unavailable")),
        t("当前支撑 ","Support ")+number(sc.get("support_floor"))+t(" / 上沿 "," / upper ")+number(sc.get("resistance_ceiling"))]
    if visible_zones:
        z=visible_zones[-1]
        structure_lines.append(t("中枢确认 ","Zone known ")+str(z.get("confirmation_time") or z.get("confirmed_at") or "unverified")[:10])
    if labels:
        structure_lines.append(t("标签 ","Source labels ")+(" / ".join(labels) if chinese else "pattern labels")+t("；同源不重复计票","; correlated labels"))
    structure_lines.append(t("锚点与确认日分开，形成中笔不作确认。","Anchors differ from confirmation; forming strokes remain provisional."))
    rv=pv.get("relative_volume20")
    close_location=w32.get("w_close_location")
    pv_lines=[t("连续供需方向 ","Continuous direction ")+str(vdir or "unverified"),
        t("相对前20日量 ","Relative volume ")+number(rv)+t("倍","x")]
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

    panels=[(.058,t("CZSC 结构","CZSC STRUCTURE"),COLORS["gold"],structure_lines),
            (.36,t("威科夫 / 量价","WYCKOFF / PRICE-VOLUME"),COLORS["ink"],pv_lines),
            (.662,t("ICT / SMC 验证","ICT / SMC VALIDATION"),COLORS["purple"],ict_lines)]
    for x,title,color,lines in panels:
        fig.add_artist(FancyBboxPatch((x-.006,.095),.28,.139,boxstyle="round,pad=.007,rounding_size=.008",
            transform=fig.transFigure,facecolor="#F7F9FB",edgecolor="none",zorder=-2))
        fig.text(x,.211,title,fontsize=12.8,color=color)
        expanded=[s for line in lines for s in wrapped_lines(line)]
        for y,line in zip([.187,.17,.153,.136,.119,.102],expanded[:6]):
            fig.text(x,y,line,fontsize=9.7,color="#41596D")
    fig.text(.058,.063,t("时钟：图中笔/中枢为当前截止的回溯几何；形成桶和未来路径不作为历史确认。P10–P90为样本分位区间，并非校准置信区间。",
        "Timing: retrospective geometry is not prior knowledge. Forming bars/forecasts cannot confirm history. P10-P90 are empirical sample quantiles."),fontsize=9.2,color=COLORS["muted"])
    provenance=t("本地用户声明数据；未独立认证行情、公司行为与成交可行性。","Local user-declared data; prices, corporate actions and execution are not independently certified.")
    fig.text(.058,.041,provenance,fontsize=9.2,color=COLORS["muted"])
    if not chinese:
        data.setdefault("render_warnings",[]).append("No CJK font available; English chart labels were used.")
    data["render"]={"style":"kcpredict-composite-v3","font":family,"language":"zh" if chinese else "en","dpi":dpi,
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
            "prediction_status":pstatus,"prediction_usable":pstatus=="pass",
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
