#!/usr/bin/env python3
"""
build_brief.py — weekly Export Sales brief (data/brief.json).

For each commodity: US commitments and shipments vs the latest USDA WASDE export forecast, pace vs
past seasons at the same week, what is needed per week to reach USDA's number, this week's buyers,
and — for destinations USDA forecasts — each importer's commitments vs its import forecast and the
US's usual share of it.
"""
import io, json, os
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ESR = ROOT / "data" / "esr"
OUT = ROOT / "data" / "brief.json"
WASDE_RAW = "https://raw.githubusercontent.com/BMR-Com/wasde-analytics/main/data"
WASDE_LOCAL = os.getenv("WASDE_LOCAL")

COMMS = [
    {"key": "cotton", "to_world": 1.0, "name": "Cotton", "codes": [1404, 1301], "wasde": "Cotton.json", "start": 8, "esr_unit": "running bales",
     "disp": 1e3, "disp_unit": "'000 running bales", "w_unit": "m 480-lb bales", "w_fmt": 2},
    # Disabled for now (kept for future use): USDA WASDE and export-sales categories need reconciling first.
    {"key": "corn", "enabled": False, "to_world": 0.0254012, "name": "Corn", "codes": [401], "wasde": "Corn.json", "start": 9, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "m bu", "w_fmt": 0},
    {"key": "soybeans", "enabled": False, "to_world": 0.0272155, "name": "Soybeans", "codes": [801], "wasde": "Oilseed,_Soybean.json", "start": 9, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "m bu", "w_fmt": 0},
    {"key": "wheat", "enabled": False, "to_world": 0.0272155, "name": "Wheat", "codes": [107], "wasde": "Wheat.json", "start": 6, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "m bu", "w_fmt": 0},
    {"key": "soymeal", "enabled": False, "name": "Soybean Meal", "codes": [901], "wasde": "Meal,_Soybean.json", "start": 10, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "k short tons", "w_fmt": 0},
    {"key": "soyoil", "enabled": False, "name": "Soybean Oil", "codes": [902], "wasde": "Oil,_Soybean.json", "start": 10, "esr_unit": "MT", "disp": 1e3, "disp_unit": "k MT", "w_unit": "m lb", "w_fmt": 0},
    {"key": "sorghum", "enabled": False, "to_world": 0.0254012, "name": "Sorghum", "codes": [701], "wasde": "Sorghum.json", "start": 9, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "m bu", "w_fmt": 0},
    {"key": "rice", "enabled": False, "to_world": 1.0, "name": "Rice", "codes": [1505], "wasde": "Rice.json", "start": 8, "esr_unit": "MT", "disp": 1e6, "disp_unit": "MMT", "w_unit": "MMT", "w_fmt": 2},
]
# ESR destination name → WASDE region name
REGION = {"Bangladesh": "Bangladesh", "Vietnam": "Vietnam", "China": "China", "Turkey": "Turkey", "Pakistan": "Pakistan",
          "Indonesia": "Indonesia", "Mexico": "Mexico", "India": "India", "Thailand": "Thailand", "Egypt": "Egypt",
          "Japan": "Japan", "South Korea": "South Korea", "Korea, South": "South Korea", "Philippines": "Philippines",
          "Nigeria": "Nigeria", "Brazil": "Brazil", "Canada": "Canada", "Burma/Myanmar": "Burma"}
ALIAS = {"Ending stocks": "Ending Stocks", "Domestic use": "Domestic Use", "Domestic, Total": "Domestic Use", "Domestic Total": "Domestic Use",
         "Total  Domestic": "Domestic Use", "Beginning stocks": "Beginning Stocks"}
# competing exporters and big importers for the supply read
# Cotton: the US competes mainly with Brazil, Australia and the African Franc Zone. India counts only in seasons when its
# own supply (beginning stocks + crop) is at least 1.5x its use — otherwise it is a net importer. China does not export.
INDIA_EXPORT_RATIO = 1.5
SUPPLY = {"cotton": (["Brazil", "Australia", "Afr. Fr. Zone", "India"], ["China", "India", "Pakistan", "Turkey", "Bangladesh", "Vietnam", "Indonesia"]),
          "corn": (["Brazil", "Argentina", "Ukraine", "South Africa", "Russia"], ["Mexico", "Japan", "South Korea", "Egypt", "China", "Southeast Asia", "European Union"]),
          "soybeans": (["Brazil", "Argentina", "Paraguay"], ["China", "European Union", "Mexico", "Japan", "Southeast Asia"]),
          "wheat": (["European Union", "Russia", "Canada", "Australia", "Argentina", "Ukraine", "Kazakhstan"], ["Egypt", "Indonesia", "China", "N. Africa", "Sel. Mideast", "Japan", "Southeast Asia", "Brazil"])}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def supply_context(cfg, w, my):
    if cfg["key"] not in SUPPLY:
        return None
    EXP, IMP = SUPPLY[cfg["key"]]; y0 = int(my[:4]); pmy = f"{y0 - 1}/{str(y0)[-2:]}"
    g = lambda r, a, m: w_latest(w, r, a, m)[0]
    ex, im = [], []
    for r in EXP:
        n = [g(r, a, my) for a in ("Beginning Stocks", "Production", "Domestic Use", "Exports")]; o = [g(r, a, pmy) for a in ("Beginning Stocks", "Production", "Domestic Use", "Exports")]
        if None in n[:3] or None in o[:3]:
            continue
        ratio = (n[0] + n[1]) / n[2] if n[2] else None; ratio_ly = (o[0] + o[1]) / o[2] if o[2] else None
        active = not (cfg["key"] == "cotton" and r == "India" and (ratio or 0) < INDIA_EXPORT_RATIO)
        ex.append({"r": r, "surplus": n[0] + n[1] - n[2], "surplus_ly": o[0] + o[1] - o[2], "prod": n[1], "prod_ly": o[1], "bs": n[0], "bs_ly": o[0], "exp": n[3], "exp_ly": o[3],
                   "ratio": ratio, "ratio_ly": ratio_ly, "active": active,
                   "active_ly": not (cfg["key"] == "cotton" and r == "India" and (ratio_ly or 0) < INDIA_EXPORT_RATIO)})
    for r in IMP:
        n = [g(r, a, my) for a in ("Beginning Stocks", "Production", "Domestic Use", "Imports")]; o = [g(r, a, pmy) for a in ("Beginning Stocks", "Production", "Domestic Use", "Imports")]
        if None in n[:3] or None in o[:3]:
            continue
        im.append({"r": r, "supply": n[0] + n[1], "supply_ly": o[0] + o[1], "use": n[2], "use_ly": o[2], "imp": n[3], "imp_ly": o[3],
                   "gap": (n[0] + n[1] - n[2]) - (o[0] + o[1] - o[2])})
    wu, wu_ly = g("World", "Domestic Use", my), g("World", "Domestic Use", pmy)
    wt, wt_ly = g("World", "Imports", my), g("World", "Imports", pmy)
    cv = cfg.get("to_world", 1.0)
    us, us_ly = (g("United States", "Exports", my) or 0) * cv, (g("United States", "Exports", pmy) or 0) * cv
    wex = g("World", "Exports", my) or wt; wex_ly = g("World", "Exports", pmy) or wt_ly
    return {"exporters": ex, "importers": im, "world_use": wu, "world_use_ly": wu_ly, "world_trade": wt, "world_trade_ly": wt_ly,
            "us_share": us / wex if us and wex else None, "us_share_ly": us_ly / wex_ly if us_ly and wex_ly else None, "my": my, "pmy": pmy}


def supply_text(S, cfg, fu):
    if not S or not S["exporters"]:
        return None
    t = ""
    act = [x for x in S["exporters"] if x["active"] or x["active_ly"]]
    d = sum((x["surplus"] if x["active"] else 0) - (x["surplus_ly"] if x["active_ly"] else 0) for x in act)
    dx = sum(((x["exp"] or 0) if x["active"] else 0) - ((x["exp_ly"] or 0) if x["active_ly"] else 0) for x in act)
    names = ", ".join(x["r"] for x in S["exporters"] if x["active"])
    tl = [f"{x['r']} {x['surplus'] - x['surplus_ly']:+.2f}" for x in sorted([x for x in act if x["active"]], key=lambda x: x["surplus"] - x["surplus_ly"]) if abs(x["surplus"] - x["surplus_ly"]) >= 0.2][:4]
    t += (f"Main competitors ({names}): exportable surplus {d:+.2f} vs {S['pmy']}" + (f" ({', '.join(tl)})" if tl else "")
          + f", and USDA has their combined exports {dx:+.2f} — " + ("less competition for US cotton. " if dx < -0.2 else "more competition for US cotton. " if dx > 0.2 else "little change in competition. "))
    ind = next((x for x in S["exporters"] if x["r"] == "India"), None)
    if ind and not ind["active"] and ind.get("ratio"):
        t += f"India is not competing for export business this season: supply is {ind['ratio']:.2f}× its own use (it exports in volume only when this is about 1.5× or more), so it is counted as an importer. "
    tight = [x for x in S["importers"] if x["gap"] < -0.3 * (1 if cfg["key"] == "cotton" else 3)]
    loose = [x for x in S["importers"] if x["gap"] > 0.3 * (1 if cfg["key"] == "cotton" else 3)]
    if tight:
        t += "Importers with tighter own supply vs use than last year: " + ", ".join(
            f"{x['r']} ({x['gap']:+.2f}" + (f"; imports {x['imp']:.2f} vs {x['imp_ly']:.2f}" if x["imp"] is not None and x["imp_ly"] is not None else "") + ")" for x in sorted(tight, key=lambda x: x["gap"])[:4]) + ". "
    if loose:
        t += "Better supplied: " + ", ".join(f"{x['r']} ({x['gap']:+.2f})" for x in sorted(loose, key=lambda x: -x["gap"])[:3]) + ". "
    if S["world_use"] and S["world_use_ly"]:
        t += f"World use {S['world_use']:,.2f} ({S['world_use'] / S['world_use_ly'] - 1:+.1%} y/y)"
        if S["world_trade"] and S["world_trade_ly"]:
            t += f", world trade {S['world_trade']:,.2f} ({S['world_trade'] / S['world_trade_ly'] - 1:+.1%})"
        t += ". "
    if S["us_share"] and S["us_share_ly"]:
        t += f"USDA has the US at {S['us_share'] * 100:.0f}% of world exports vs {S['us_share_ly'] * 100:.0f}% last season"
        if cfg["key"] == "wheat":
            t += "."
        elif dx < -0.2 and S["us_share"] <= S["us_share_ly"]:
            t += " — **with competitors tighter, that share looks conservative.**"
        elif dx > 0.2 and S["us_share"] > S["us_share_ly"]:
            t += " — **with competitors better supplied, that share looks ambitious.**"
        else:
            t += "."
    return t


# ─────────────────────────── data ───────────────────────────
def wasde(fname):
    if WASDE_LOCAL:
        d = json.load(open(Path(WASDE_LOCAL) / fname))
    else:
        import requests
        r = requests.get(f"{WASDE_RAW}/{fname}", timeout=120); r.raise_for_status(); d = r.json()
    rows = []
    for k, vals in d["data"].items():
        reg, attr, my = k.split("||")
        for v in vals:
            if v.get("value") is not None:
                rows.append((reg, ALIAS.get(attr, attr), my, pd.Timestamp(year=v["forecastYear"], month=v["forecastMonth"], day=1), float(v["value"])))
    w = pd.DataFrame(rows, columns=["reg", "attr", "my", "rd", "val"])
    return w.drop_duplicates(["reg", "attr", "my", "rd"], keep="last")


def w_latest(w, reg, attr, my, upto=None):
    x = w[(w.reg == reg) & (w.attr == attr) & (w.my == my)]
    if upto is not None:
        x = x[x.rd <= upto]
    x = x.sort_values("rd")
    return (float(x.val.iloc[-1]), x.rd.iloc[-1]) if len(x) else (None, None)


PRICE_RAW = "https://raw.githubusercontent.com/BMR-Com/cftc-cot-full/main/data/prices/daily.csv"
PRICE_LOCAL = os.getenv("PRICE_LOCAL")
# buyers whose weekly purchases historically respond most to price moves in the reporting week
PRICE_SENSITIVE = ["Vietnam", "Turkey", "Pakistan", "China", "Taiwan", "Thailand", "India", "Indonesia"]


def ice_cotton():
    try:
        if PRICE_LOCAL:
            p = pd.read_csv(PRICE_LOCAL, parse_dates=["date"])
        else:
            import requests
            r = requests.get(PRICE_RAW, timeout=120); r.raise_for_status(); p = pd.read_csv(io.BytesIO(r.content), parse_dates=["date"])
        return p[p.ticker == "CT"].set_index("date").sort_index()[["close", "low", "high"]].dropna()
    except Exception as ex:
        print("price data unavailable:", ex); return None


def week_px(px, we):
    """ICE cotton (front month) over the export-sales reporting week: Friday to Thursday ending `we`."""
    s = px[(px.index > we - pd.Timedelta(days=7)) & (px.index <= we)]
    return {"avg": float(s.close.mean()), "low": float(s.low.min()), "high": float(s.high.max()), "close": float(s.close.iloc[-1]), "days": len(s)} if len(s) else None


def esr(codes):
    parts = []
    for c in codes:
        f = ROOT / "cotton_history_1404.csv" if c == 1404 else ESR / f"{c}.csv"
        if f.exists():
            parts.append(pd.read_csv(f, low_memory=False))
    if not parts:
        return None
    e = pd.concat(parts)
    e["weekEndingDate"] = pd.to_datetime(e.weekEndingDate)
    for c in ["grossNewSales", "currentMYNetSales", "weeklyExports", "accumulatedExports", "outstandingSales", "nextMYNetSales", "nextMYOutstandingSales"]:
        e[c] = pd.to_numeric(e[c], errors="coerce").fillna(0)
    cn = json.load(open(ESR / "countries.json")) if (ESR / "countries.json").exists() else {}
    e["countryName"] = [cn.get(str(int(c)), str(n).strip().title()) for c, n in zip(pd.to_numeric(e.countryCode, errors="coerce").fillna(0), e.countryName)]
    e = e[e.countryCode.astype(str) != "0"]
    e["commit"] = e.accumulatedExports + e.outstandingSales
    return e


def my_label(e, start_month):
    """ESR marketYear → WASDE 'YYYY/YY' using the first week of each year's data."""
    out = {}
    for y, g in e.groupby("marketYear"):
        d0 = g.weekEndingDate.min()
        sy = d0.year if d0.month >= start_month else d0.year - 1
        out[y] = (sy, f"{sy}/{str(sy + 1)[-2:]}")
    return out


# ─────────────────────────── analysis ───────────────────────────
def q(v):
    """Format a value in display units ('000 bales): no decimals above 100, one below."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


def build(cfg):
    e = esr(cfg["codes"])
    if e is None or e.empty:
        return None
    w = wasde(cfg["wasde"])
    labels = my_label(e, cfg["start"])
    tot = e.groupby(["marketYear", "weekEndingDate"])[["commit", "accumulatedExports", "outstandingSales", "currentMYNetSales", "weeklyExports",
                                                       "grossNewSales", "nextMYOutstandingSales", "nextMYNetSales"]].sum().reset_index()
    tot["wk"] = tot.apply(lambda r: (r.weekEndingDate - pd.Timestamp(year=labels[r.marketYear][0], month=cfg["start"], day=1)).days // 7, axis=1)
    cur_y = int(tot.marketYear.max()); sy, my = labels[cur_y]
    c = tot[tot.marketYear == cur_y].sort_values("weekEndingDate")
    if c.empty:
        return None
    L = c.iloc[-1]; wk = int(L.wk); last_date = L.weekEndingDate
    target, rd = w_latest(w, "United States", "Exports", my)
    if target is None:
        return None
    prev_t, _ = w_latest(w, "United States", "Exports", my, rd - pd.Timedelta(days=20))

    # ratio ESR final shipments / WASDE final exports (per completed season), and same-week pace history
    hist = []
    for y in sorted(tot.marketYear.unique()):
        if y >= cur_y or y < cur_y - 10:
            continue
        g = tot[tot.marketYear == y].sort_values("wk")
        if g.empty:
            continue
        fin_esr = float(g.accumulatedExports.iloc[-1]); wfin, _ = w_latest(w, "United States", "Exports", labels[y][1])
        at = g[g.wk <= wk]
        if not len(at) or not wfin or not fin_esr:
            continue
        a = at.iloc[-1]
        hist.append({"y": y, "my": labels[y][1], "ratio": fin_esr / wfin, "fin_esr": fin_esr, "wfin": wfin,
                     "commit_pct": a.commit / fin_esr, "ship_pct": a.accumulatedExports / fin_esr, "commit": a.commit,
                     "net4": float(g[(g.wk > wk - 4) & (g.wk <= wk)].currentMYNetSales.mean())})
    H = pd.DataFrame(hist)
    if H.empty:
        return None
    H5 = H.tail(5)
    ratio = float(H5.ratio.median()); t_esr = target * ratio
    pace = L.commit / t_esr; pace_n = float(H5.commit_pct.mean()); ship = L.accumulatedExports / t_esr; ship_n = float(H5.ship_pct.mean())
    implied = L.commit / pace_n / ratio if pace_n > 0 else None
    weeks_left = max(1, 52 - (wk + 1))
    need_ship = (t_esr - L.accumulatedExports) / weeks_left
    CARRY = 1_000_000 if cfg["key"] == "cotton" else 0      # assumed sales carried into next season (running bales)
    sales_left = t_esr + CARRY - L.commit; ship_left = t_esr - L.accumulatedExports
    need_sales = sales_left / weeks_left
    rec = c.tail(4); net4 = float(rec.currentMYNetSales.mean()); ship4 = float(rec.weeklyExports.mean())
    ly = H[H.y == cur_y - 1]; ly_commit = float(ly.commit.iloc[0]) if len(ly) else None
    net4_5y = float(H5.net4.mean())
    D = cfg["disp"]

    # this week's buyers
    tw = e[(e.marketYear == cur_y) & (e.weekEndingDate == last_date)]
    by = tw.groupby("countryName")[["currentMYNetSales", "weeklyExports", "nextMYNetSales"]].sum()
    buyers = by.currentMYNetSales.sort_values(ascending=False)
    top_buy = [{"c": k, "v": round(v / D, 3)} for k, v in buyers.head(5).items() if v > 0]
    cancels = [{"c": k, "v": round(v / D, 3)} for k, v in buyers.tail(3).items() if v / D <= -0.0005]
    top_ship = [{"c": k, "v": round(v / D, 3)} for k, v in by.weeklyExports.sort_values(ascending=False).head(5).items() if v > 0]

    # destinations
    cy = e[e.marketYear == cur_y]
    cn = cy[cy.weekEndingDate == last_date].groupby("countryName").commit.sum()
    dest = []
    names = list(cn.sort_values(ascending=False).head(12).index)
    for reg in sorted(set(REGION.values())):
        imp, _ = w_latest(w, reg, "Imports", my)
        if imp and imp > (0.5 if cfg["key"] == "cotton" else 0.3):
            for k_, v_ in REGION.items():
                if v_ == reg and k_ in set(cy.countryName) and k_ not in names:
                    names.append(k_)
    for nm in names:
        row = {"c": nm, "commit": round(float(cn.get(nm, 0)) / D, 3), "share_now": float(cn.get(nm, 0)) / L.commit if L.commit else None}
        hh = []
        for _, h in H.iterrows():
            g = e[(e.marketYear == h.y) & (e.countryName == nm)]
            if g.empty:
                hh.append({"y": h.y, "my": h.my, "same": 0.0, "fin": 0.0}); continue
            gw = g.groupby("weekEndingDate")[["commit", "accumulatedExports"]].sum().reset_index()
            gw["wk"] = (gw.weekEndingDate - pd.Timestamp(year=labels[h.y][0], month=cfg["start"], day=1)).dt.days // 7
            at = gw[gw.wk <= wk]
            hh.append({"y": h.y, "my": h.my, "same": float(at.commit.iloc[-1]) if len(at) else 0.0, "fin": float(gw.sort_values("wk").accumulatedExports.iloc[-1]),
                       "ratio": h.ratio})
        hh = pd.DataFrame(hh)
        last5 = hh.tail(5)
        row["ly"] = round(float(hh.same.iloc[-1]) / D, 3) if len(hh) else None
        row["avg5"] = round(float(last5.same.mean()) / D, 3) if len(last5) else None
        fin5 = last5.fin.sum()
        row["norm_pct"] = float(last5.same.sum() / fin5) if fin5 > 0 else None   # share of final usually committed by now
        reg = REGION.get(nm)
        if reg:
            imp, _ = w_latest(w, reg, "Imports", my)
            sh = []
            for _, x in hh.iterrows():
                ii, _ = w_latest(w, reg, "Imports", x.my)
                if ii and ii > 0 and x.get("ratio"):
                    sh.append(x.fin / x.ratio * cfg.get("to_world", 1.0) / ii)
            if imp and sh:
                s5, s10 = float(np.mean(sh[-5:])), float(np.mean(sh))
                exp_us = imp * s5                             # usual US-origin volume (WASDE units)
                row.update({"imports": imp, "us_share5": s5, "us_share10": s10, "exp_us": round(exp_us, 3)})
                if row["norm_pct"] and row["norm_pct"] >= 0.25:
                    impl = float(cn.get(nm, 0)) / row["norm_pct"] / ratio * cfg.get("to_world", 1.0)
                    row["implied_us"] = round(impl, 3); row["implied_share"] = impl / imp
                    row["vs_usual"] = impl / exp_us - 1 if exp_us else None
        dest.append(row)

    # shipment seasonality by destination: share of the season's shipments by calendar month (last 5 seasons)
    seas = []
    ye = e[e.marketYear.isin(H5.y)].copy(); ye["m"] = ye.weekEndingDate.dt.month
    top = ye.groupby("countryName").weeklyExports.sum().sort_values(ascending=False)
    for nm in [n for n in top.index if n not in ("Unknown", "Code 9990", "Unknown destinations")][:10]:
        g = ye[ye.countryName == nm].groupby("m").weeklyExports.sum()
        if g.sum() <= 0:
            continue
        sh = (g / g.sum()).reindex(range(1, 13), fill_value=0)
        order = [((cfg["start"] - 1 + i) % 12) + 1 for i in range(12)]
        cum = np.cumsum([sh[m] for m in order])
        mi = order.index(last_date.month)
        cy_c = cy[(cy.countryName == nm) & (cy.weekEndingDate == last_date)]
        acc = float(cy_c.accumulatedExports.sum()); com = float(cy_c.commit.sum())
        seas.append({"c": nm, "share": [round(float(sh[m]), 3) for m in order], "peak": [MONTHS[m - 1] for m in sh.sort_values(ascending=False).index[:3]],
                     "norm_shipped": round(float(cum[mi]), 3), "shipped_of_commit": round(acc / com, 3) if com else None,
                     "next3": round(float(sum(sh[order[(mi + k) % 12]] for k in range(1, 4))), 3), "outstanding": round((com - acc) / D, 3)})
    months_order = [MONTHS[((cfg["start"] - 1 + i) % 12)] for i in range(12)]
    S = supply_context(cfg, w, my)

    # narrative
    fu = lambda v: f"{v:,.{cfg['w_fmt']}f}"
    T = []
    head = (f"Week ending {last_date:%b %d}: {cfg['name'].lower()} commitments {q(L.commit / D)} {cfg['disp_unit']} for {my} "
            f"({q(L.accumulatedExports / D)} shipped, {q(L.outstandingSales / D)} outstanding) = **{pace*100:.0f}% of USDA's {fu(target)} {cfg['w_unit']}** "
            f"export forecast, vs a 5-yr norm of {pace_n*100:.0f}% by week {wk + 1}. "
            + (f"(USDA's statistical bales are converted to the export-sales running-bale basis at {ratio / 1e6:.3f} running bales per 480-lb bale, the median ratio of final export-sales shipments to USDA's final exports over the last 5 seasons: {fu(target)}m ≈ {q(t_esr / D)} {cfg['disp_unit']}.) " if cfg["key"] == "cotton" else ""))
    if ly_commit:
        head += f"Commitments are {L.commit / ly_commit - 1:+.0%} vs a year ago. "
    if implied:
        gap = implied / target - 1
        head += (f"At the usual seasonal pace that points to about **{fu(implied)} {cfg['w_unit']}** ({gap:+.0%} vs USDA)"
                 + ((" — early-season sales are a weak guide to the final number for this crop, so treat this as a direction, not a forecast." if cfg["key"] in ("corn", "sorghum") and abs(gap) > 0.04 else "")
                    or (" — USDA looks too high; risk of a cut." if gap < -0.04 else " — room for USDA to raise." if gap > 0.04 else " — in line with USDA.")))
    T.append({"h": "Pace vs USDA", "t": head})
    t = (f"Shipments {ship*100:.0f}% of target (norm {ship_n*100:.0f}%). To reach USDA's number, shipments need to average "
         f"{q(need_ship / D)} {cfg['disp_unit']}/week for the remaining {weeks_left} weeks vs {q(ship4 / D)} over the last 4 weeks; "
         f"new sales need {q(max(need_sales, 0) / D)}/week (allowing for about {q(CARRY / D)} carried into next season) vs {q(net4 / D)} recently (5-yr avg for these weeks {q(net4_5y / D)}).")
    T.append({"h": "What USDA's number requires", "t": t})
    if prev_t is not None and abs(target - prev_t) > 1e-9:
        T.append({"h": "Latest WASDE", "t": f"USDA {'raised' if target > prev_t else 'cut'} the {my} export forecast to {fu(target)} {cfg['w_unit']} ({target - prev_t:+,.{cfg['w_fmt']}f}) in the {rd:%B} report."})
    wkline = f"Net sales {q(L.currentMYNetSales / D)} {cfg['disp_unit']} (4-wk avg {q(net4 / D)}); shipments {q(L.weeklyExports / D)}."
    if top_buy:
        wkline += " Main buyers: " + ", ".join(f"{b['c']} {q(b['v'])}" for b in top_buy[:4]) + "."
    if cancels:
        wkline += " Net reductions: " + ", ".join(f"{b['c']} {q(b['v'])}" for b in cancels) + "."
    if L.nextMYOutstandingSales > 0 and wk > 30:
        wkline += f" Next-season sales on the books {q(L.nextMYOutstandingSales / D)}."
    T.append({"h": "This week", "t": wkline})
    xs = 0.0
    dd = [d for d in dest if d.get("vs_usual") is not None and d.get("imports") and np.isfinite(d["vs_usual"])]
    if dd:
        ahead = sorted([d for d in dd if d["vs_usual"] > 0.15], key=lambda d: -d["vs_usual"])[:3]
        behind = sorted([d for d in dd if d["vs_usual"] < -0.15], key=lambda d: d["vs_usual"])[:3]
        t = ""
        if ahead:
            t += "Buying more US than usual: " + "; ".join(f"{d['c']} (pace implies {d['implied_share']*100:.0f}% US share of its {d['imports']:,.2f} import forecast vs {d['us_share5']*100:.0f}% usual)" for d in ahead) + ". "
        if behind:
            t += "Buying less US than usual: " + "; ".join(f"{d['c']} ({d['implied_share']*100:.0f}% vs {d['us_share5']*100:.0f}% usual)" for d in behind) + "."
        xs = sum((d["implied_us"] - d["exp_us"]) for d in dd) / cfg.get("to_world", 1.0)
        t += (f" Across these destinations, the sales pace adds up to {fu(xs) if abs(xs) >= 10 else f'{xs:+.2f}'} {cfg['w_unit']} vs their usual US-origin volumes"
              + (" — supports a higher US export forecast." if xs > 0.03 * target else " — argues against raising USDA's number." if xs < -0.03 * target else "."))
        if t:
            T.append({"h": "Destinations vs import forecasts", "t": t})
    st = supply_text(S, cfg, fu)
    if st:
        T.append({"h": "Competition and import need", "t": st})
    if seas:
        nxt = sorted(seas, key=lambda x: -x["next3"])[:3]
        lag = [x for x in seas if x["shipped_of_commit"] is not None and x["outstanding"] > 0 and x["norm_shipped"] > 0.25 and x["shipped_of_commit"] < 0.5 * x["norm_shipped"]]
        t = ("Shipments usually concentrate in the next three months for " + ", ".join(f"{x['c']} ({x['next3']*100:.0f}% of its season)" for x in nxt) + ".")
        if lag:
            t += " Slow to ship vs sales: " + ", ".join(f"{x['c']} ({q(x['outstanding'])} outstanding)" for x in lag[:3]) + " — watch for cancellations or roll-overs."
        T.append({"h": "Shipment calendar", "t": t})
    # ── week-to-week and year-to-date scorecard ──
    FLOW = [("grossNewSales", "Gross new sales"), ("currentMYNetSales", "Net sales"), ("weeklyExports", "Shipments"), ("nextMYNetSales", "Next-season net sales")]
    STOCK = [("accumulatedExports", "Shipped to date"), ("outstandingSales", "Outstanding sales"), ("commit", "Total commitments"), ("nextMYOutstandingSales", "Next-season outstanding")]
    yrs5 = list(H5.y)
    def make_score(tt):
        cc = tt[tt.marketYear == cur_y].sort_values("weekEndingDate")
        cc = cc[cc.weekEndingDate <= last_date]
        if cc.empty:
            return []
        LL = cc.iloc[-1]; pv = cc.iloc[-2] if len(cc) > 1 else None
        def same_week(y, col):
            g = tt[(tt.marketYear == y) & (tt.wk <= wk)].sort_values("wk")
            return float(g[col].iloc[-1]) if len(g) else None
        def ytd(y, col):
            g = tt[(tt.marketYear == y) & (tt.wk <= wk)]
            return float(g[col].sum()) if len(g) else None
        out_ = []
        for col, lab in FLOW:
            lyv = same_week(cur_y - 1, col); a5 = [v for v in (same_week(y, col) for y in yrs5) if v is not None]
            yt = float(cc[col].sum()); yl = ytd(cur_y - 1, col); y5 = [v for v in (ytd(y, col) for y in yrs5) if v is not None]
            out_.append({"k": col, "label": lab, "type": "flow", "tw": float(LL[col]) / D, "lw": float(pv[col]) / D if pv is not None else None,
                         "avg4": float(cc[col].tail(4).mean()) / D, "ly": lyv / D if lyv is not None else None, "avg5": float(np.mean(a5)) / D if a5 else None,
                         "ytd": yt / D, "ytd_ly": yl / D if yl is not None else None, "ytd_avg5": float(np.mean(y5)) / D if y5 else None})
        for col, lab in STOCK:
            lyv = same_week(cur_y - 1, col); a5 = [v for v in (same_week(y, col) for y in yrs5) if v is not None]
            out_.append({"k": col, "label": lab, "type": "stock", "tw": float(LL[col]) / D, "lw": float(pv[col]) / D if pv is not None else None,
                         "ly": lyv / D if lyv is not None else None, "avg5": float(np.mean(a5)) / D if a5 else None})
        return out_
    prev = c.iloc[-2] if len(c) > 1 else None
    score = make_score(tot)
    score_by = {"All": score}
    if len(cfg["codes"]) > 1:
        names_ = {1404: "Upland", 1301: "Pima"}
        for code in cfg["codes"]:
            sub = e[e.commodityCode.astype(int) == code]
            if sub.empty:
                continue
            ts = sub.groupby(["marketYear", "weekEndingDate"])[["commit", "accumulatedExports", "outstandingSales", "currentMYNetSales", "weeklyExports",
                                                                 "grossNewSales", "nextMYOutstandingSales", "nextMYNetSales"]].sum().reset_index()
            ts["wk"] = [(d - pd.Timestamp(year=labels[y][0], month=cfg["start"], day=1)).days // 7 for y, d in zip(ts.marketYear, ts.weekEndingDate)]
            score_by[names_.get(code, str(code))] = make_score(ts)
    # weekly series (this season + last 5) for interactive charts
    series = {}
    yrs20 = [y for y in sorted(tot.marketYear.unique()) if y < cur_y][-20:]
    for y in [cur_y] + yrs20[::-1]:
        g = tot[tot.marketYear == y].sort_values("wk")
        series[labels[y][1]] = {k: [[int(w_) + 1, round(float(v) / D, 4)] for w_, v in zip(g.wk, g[k])] for k, _ in FLOW + STOCK}
    # countries this week
    cw = e[(e.marketYear == cur_y)]
    tw_ = cw[cw.weekEndingDate == last_date].groupby("countryName")[["currentMYNetSales", "weeklyExports", "accumulatedExports", "outstandingSales", "commit", "nextMYOutstandingSales"]].sum()
    lw_ = cw[cw.weekEndingDate == (prev.weekEndingDate if prev is not None else last_date)].groupby("countryName")[["currentMYNetSales", "weeklyExports"]].sum()
    lyc = {}
    gly = e[e.marketYear == cur_y - 1].copy()
    if len(gly):
        gly["wk"] = (gly.weekEndingDate - pd.Timestamp(year=labels[cur_y - 1][0], month=cfg["start"], day=1)).dt.days // 7
        lastw = gly[gly.wk <= wk].weekEndingDate.max()
        lyc = gly[gly.weekEndingDate == lastw].groupby("countryName")[["commit", "accumulatedExports"]].sum().to_dict("index")
    COLS = ["currentMYNetSales", "weeklyExports", "accumulatedExports", "outstandingSales", "commit", "nextMYOutstandingSales"]
    lyw = {}
    if len(gly):
        lyw = gly[gly.weekEndingDate == lastw].groupby("countryName")[COLS].sum()
    dmap = {d["c"]: d for d in dest}
    def ship_norm(nm):
        """Share of a destination's season shipments usually made by this week (last 5 seasons)."""
        a_, f_ = 0.0, 0.0
        for y in yrs5:
            g = e[(e.marketYear == y) & (e.countryName == nm)]
            if g.empty:
                continue
            gw = g.groupby("weekEndingDate").accumulatedExports.sum().reset_index()
            gw["wk"] = (gw.weekEndingDate - pd.Timestamp(year=labels[y][0], month=cfg["start"], day=1)).dt.days // 7
            at = gw[gw.wk <= wk]
            a_ += float(at.accumulatedExports.iloc[-1]) if len(at) else 0.0; f_ += float(gw.accumulatedExports.max())
        return a_ / f_ if f_ > 0 else None
    def rowfor(nm, cur_, lw0, ly0):
        g = lambda df, k: float(df.get(k, 0)) / D if df is not None else None
        r = {"c": nm, "net": g(cur_, "currentMYNetSales"), "net_lw": g(lw0, "currentMYNetSales"), "net_ly": g(ly0, "currentMYNetSales"),
             "ship": g(cur_, "weeklyExports"), "ship_lw": g(lw0, "weeklyExports"), "ship_ly": g(ly0, "weeklyExports"),
             "accum": g(cur_, "accumulatedExports"), "accum_ly": g(ly0, "accumulatedExports"),
             "outst": g(cur_, "outstandingSales"), "outst_ly": g(ly0, "outstandingSales"),
             "commit": g(cur_, "commit"), "commit_ly": g(ly0, "commit"), "next": g(cur_, "nextMYOutstandingSales")}
        d = dmap.get(nm)
        if d and d.get("imports") and d.get("us_share5"):
            exp_season = d["imports"] * d["us_share5"] / cfg.get("to_world", 1.0) * ratio / D   # usual US volume, export-sales basis
            sn = ship_norm(nm)
            r.update({"imports": d["imports"], "us_share5": d["us_share5"], "exp_season": exp_season, "ship_norm": sn,
                      "exp_mytd": exp_season * sn if sn is not None else None})
        return r
    TW = tw_.reindex(columns=COLS).fillna(0)
    LW = cw[cw.weekEndingDate == (prev.weekEndingDate if prev is not None else last_date)].groupby("countryName")[COLS].sum()
    order_ = [n for n in TW.sort_values("commit", ascending=False).index if n != "Unknown destinations"]
    top12 = order_[:12]
    ctab = [rowfor(nm, TW.loc[nm], LW.loc[nm] if nm in LW.index else None, lyw.loc[nm] if len(lyw) and nm in lyw.index else None) for nm in top12]
    rest = [n for n in TW.index if n not in top12]
    ly_rest = lyw[~lyw.index.isin(top12)].sum() if len(lyw) else None
    oth = rowfor(f"Others ({len(rest)})", TW.loc[rest].sum(), LW[LW.index.isin(rest)].sum(), ly_rest)
    oth["others"] = True
    ctab.append(oth)
    sc = {x["k"]: x for x in score}
    def ch(a, b):
        return (lambda r: "0%" if abs(r) < 0.005 else f"{r:+.0%}")(a / b - 1) if a is not None and b not in (None, 0) else "n/a"
    ns, sh_ = sc["currentMYNetSales"], sc["weeklyExports"]
    T.insert(2, {"h": "Week on week", "t": (
        f"Net sales {q(ns['tw'])} vs {q(ns['lw'] or 0)} the week before ({ch(ns['tw'], ns['lw'])}), {ch(ns['tw'], ns['ly'])} vs the same week last year and "
        f"{ch(ns['tw'], ns['avg5'])} vs the 5-yr average. Shipments {q(sh_['tw'])} vs {q(sh_['lw'] or 0)} ({ch(sh_['tw'], sh_['lw'])}), "
        f"{ch(sh_['tw'], sh_['ly'])} vs last year, {ch(sh_['tw'], sh_['avg5'])} vs the 5-yr average.")})
    T.insert(3, {"h": "Season to date", "t": (
        f"Net sales {q(ns['ytd'])} ({ch(ns['ytd'], ns['ytd_ly'])} y/y, {ch(ns['ytd'], ns['ytd_avg5'])} vs 5-yr avg); shipments {q(sc['accumulatedExports']['tw'])} "
        f"({ch(sc['accumulatedExports']['tw'], sc['accumulatedExports']['ly'])} y/y); outstanding sales {q(sc['outstandingSales']['tw'])} "
        f"({ch(sc['outstandingSales']['tw'], sc['outstandingSales']['ly'])} y/y); next-season sales on the books {q(sc['nextMYOutstandingSales']['tw'])}.")})

    # ── price in the reporting week ──
    px = ice_cotton() if cfg["key"] == "cotton" else None
    price = None
    if px is not None:
        lyd = gly[gly.wk <= wk].weekEndingDate.max() if len(gly) else None
        tw_p, lw_p = week_px(px, last_date), week_px(px, last_date - pd.Timedelta(days=7))
        ly_p = week_px(px, lyd) if lyd is not None and pd.notna(lyd) else None
        p4 = px[(px.index > last_date - pd.Timedelta(days=35)) & (px.index <= last_date - pd.Timedelta(days=7))]
        if tw_p and lw_p:
            wk_series = []
            for r in c.itertuples():
                wp = week_px(px, r.weekEndingDate)
                wk_series.append([int(r.wk) + 1, round(wp["avg"], 2) if wp else None])
            price = {"tw": tw_p, "lw": lw_p, "ly": ly_p, "avg4": float(p4.close.mean()) if len(p4) else None,
                     "chg": tw_p["avg"] / lw_p["avg"] - 1, "low_vs_lw": tw_p["low"] / lw_p["avg"] - 1,
                     "yoy": tw_p["avg"] / ly_p["avg"] - 1 if ly_p else None, "weekly": wk_series}
            cc_ = c[c.wk >= 2]                      # skip the first weeks of the season (carry-in from the old crop)
            trend = float(cc_.currentMYNetSales.iloc[-9:-1].mean()) if len(cc_) > 3 else None
            ns_now = float(L.currentMYNetSales)
            rel_ = ns_now / trend - 1 if trend else None
            t = (f"ICE cotton averaged {tw_p['avg']:.2f}¢ in the reporting week (Fri–Thu; low {tw_p['low']:.2f}, high {tw_p['high']:.2f}), "
                 f"{price['chg']:+.1%} vs the week before ({lw_p['avg']:.2f}¢)"
                 + (f" and {price['yoy']:+.1%} vs the same week last year ({ly_p['avg']:.2f}¢)" if ly_p else "") + ". "
                 f"The week's low was {price['low_vs_lw']:+.1%} vs last week's average. ")
            a5_ = [x for x in score if x["k"] == "currentMYNetSales"][0]["avg5"] if score else None
            rel5 = (ns_now / D) / a5_ - 1 if a5_ else None
            if rel_ is not None:
                t += (f"Net sales were {rel_:+.0%} vs their recent trend" + (f" and {rel5:+.0%} vs the 5-yr average for this week" if rel5 is not None else "") + ". ")
                rel_ = (rel_ + rel5) / 2 if rel5 is not None else rel_
                if price["chg"] <= -0.015:
                    t += ("**Buyers stepped in on the price dip — a sign of underlying demand.** " if rel_ > 0.2 else
                          "Some buying came in on the dip, though not strongly. " if rel_ > 0.05 else
                          "**Lower prices failed to pull in buying — demand looks soft.** " if rel_ < -0.1 else "Buying held steady on the dip. ")
                elif price["chg"] >= 0.015:
                    t += ("**Sales held up despite higher prices — firm demand.** " if rel_ > -0.05 else
                          "Buyers stepped back as prices rose, as they usually do. ")
                else:
                    t += "Prices were little changed, so the week's sales mostly reflect underlying demand. "
            sens = [b for b in top_buy if b["c"] in PRICE_SENSITIVE]
            if sens:
                t += "Price-sensitive buyers active this week: " + ", ".join(f"{b['c']} {q(b['v'])}" for b in sens) + "."
            T.insert(4, {"h": "Price and buying", "t": t})
            for r_ in ctab:
                r_["price_sensitive"] = r_["c"] in PRICE_SENSITIVE

    # ── historical profile at the same week: 5 / 10 / 20 seasons ──
    PROF = [("accumulatedExports", "Shipped to date"), ("outstandingSales", "Outstanding sales"), ("commit", "Total commitments"), ("nextMYOutstandingSales", "Next-season sales")]
    past = [y for y in sorted(tot.marketYear.unique()) if y < cur_y][-20:]
    def at_week(df_y, y):
        g = df_y.copy(); g["wk"] = (g.weekEndingDate - pd.Timestamp(year=labels[y][0], month=cfg["start"], day=1)).dt.days // 7
        g = g[g.wk <= wk]
        return g[g.weekEndingDate == g.weekEndingDate.max()] if len(g) else g.iloc[0:0]
    snap = {y: at_week(e[e.marketYear == y], y) for y in past}
    cur_snap = e[(e.marketYear == cur_y) & (e.weekEndingDate == last_date)]
    profile = {}
    for col, lab_ in PROF:
        rows_ = []
        for nm in ["Total"] + top12:
            def val(df):
                d_ = df if nm == "Total" else df[df.countryName == nm]
                return float(d_[col].sum()) / D
            hist_ = [val(snap[y]) for y in past]
            now_ = val(cur_snap)
            rec = {"c": nm, "now": now_}
            for n_ in (5, 10, 20):
                h_ = hist_[-n_:]
                if h_:
                    rec[f"min{n_}"], rec[f"max{n_}"], rec[f"avg{n_}"] = min(h_), max(h_), float(np.mean(h_))
            rec["rank"] = int(sum(v > now_ for v in hist_) + 1); rec["of"] = len(hist_) + 1
            rows_.append(rec)
        profile[col] = {"label": lab_, "rows": rows_}

    # bottom line: weigh sales pace, destinations and competitor supply
    sig = []
    if implied:
        g_ = implied / target - 1; sig.append(("sales pace", -1 if g_ < -0.04 else 1 if g_ > 0.04 else 0))
    if dd:
        sig.append(("destination buying", -1 if xs < -0.03 * target else 1 if xs > 0.03 * target else 0))
    if S and S.get("exporters"):
        dxc = sum(((x["exp"] or 0) if x["active"] else 0) - ((x["exp_ly"] or 0) if x["active_ly"] else 0) for x in S["exporters"])
        sig.append(("competitor exports", 1 if dxc < -0.03 * target else -1 if dxc > 0.03 * target else 0))
    neg = [a for a, v in sig if v < 0]; pos = [a for a, v in sig if v > 0]; net = sum(v for _, v in sig)
    bl = ("Export outlook vs USDA: **" + ("below" if net < 0 else "above" if net > 0 else "balanced around") + f" USDA's {fu(target)} {cfg['w_unit']}**. ")
    if neg:
        bl += "Pointing lower: " + ", ".join(neg) + ". "
    if pos:
        bl += "Pointing higher: " + ", ".join(pos) + ". "
    if neg and pos:
        bl += ("Less competition usually shows up later in the season, once rival origins run short — "
               "watch whether US sales accelerate into the new year before reading the slow start as lost demand.")
    T.append({"h": "Bottom line", "t": bl})
    tilt = "strong" if implied and implied / target > 1.04 else "weak" if implied and implied / target < 0.96 else "on track"
    return {"name": cfg["name"], "w_fmt": cfg["w_fmt"], "my": my, "week": f"{last_date:%Y-%m-%d}", "wk": wk + 1, "unit": cfg["disp_unit"], "w_unit": cfg["w_unit"],
            "report": f"{rd:%B %Y}", "ratio": ratio / 1e6 if cfg["key"] == "cotton" else ratio, "target": target, "target_prev": prev_t, "target_esr": round(t_esr / D, 3),
            "commit": round(L.commit / D, 3), "shipped": round(L.accumulatedExports / D, 3), "outstanding": round(L.outstandingSales / D, 3),
            "pace": pace, "pace_norm": pace_n, "ship_pct": ship, "ship_norm": ship_n, "implied": implied, "ly_commit": round(ly_commit / D, 3) if ly_commit else None,
            "net": round(L.currentMYNetSales / D, 3), "net4": round(net4 / D, 3), "net4_5y": round(net4_5y / D, 3), "ship_wk": round(L.weeklyExports / D, 3), "ship4": round(ship4 / D, 3),
            "need_ship": round(need_ship / D, 3), "need_sales": round(max(need_sales, 0) / D, 3), "weeks_left": weeks_left, "tilt": tilt,
            "score": score, "score_by": score_by, "sales_left": sales_left / D, "ship_left": ship_left / D, "carry": CARRY / D,
            "target_ly": w_latest(w, "United States", "Exports", f"{int(my[:4]) - 1}/{my[2:4]}")[0], "series": series, "countries": ctab, "price": price, "profile": profile, "profile_years": len(past), "buyers": top_buy, "cancels": cancels, "shippers": top_ship, "dest": dest, "brief": T, "seas": seas, "months": months_order, "supply": S,
            "curve": {"cur": [[int(r.wk) + 1, round(r.commit / t_esr, 4)] for r in c.itertuples()],
                      "hist": {labels[y][1]: [[int(r.wk) + 1, round(r.commit / float(tot[tot.marketYear == y].accumulatedExports.max()), 4)]
                                              for r in tot[tot.marketYear == y].sort_values("wk").itertuples()]
                               for y in yrs20[::-1] if tot[tot.marketYear == y].accumulatedExports.max() > 0}}}


def main():
    out = {"generated": pd.Timestamp.now("UTC").strftime("%Y-%m-%d %H:%M UTC"), "commodities": []}
    for cfg in [c for c in COMMS if c.get("enabled", True)]:
        try:
            b = build(cfg)
        except Exception as ex:
            print(cfg["name"], "failed:", repr(ex)); b = None
        if b:
            out["commodities"].append(b)
            print(f"\n== {b['name']} {b['my']} wk{b['wk']} ({b['week']})")
            for x in b["brief"]:
                print(f"[{x['h']}] {x['t']}")
    OUT.write_text(json.dumps(out, separators=(",", ":"), default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else float(o)))


if __name__ == "__main__":
    main()
