"""
IMF Real Exchange Rate Explorer
===============================

Streamlit app that pulls monthly CPI and exchange-rate data from the IMF
SDMX 3.0 API and computes a bilateral real exchange rate (RER).

Definitions
-----------
E_i(t)  : IMF exchange rate for country i, local-currency units per USD
S(t)    : units of Country 2 currency per one unit of Country 1 currency
          S(t) = E2(t) / E1(t)
P_i(t)  : monthly CPI index for country i
RER(t)  : S(t) * P1(t) / P2(t)
RER*    : average of RER(t) over valid months in the selected window
Real Index(t) = 100 * RER(t) / RER*

Compatible with Python 3.12.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
IMF_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
HEADERS = {"Accept": "application/json", "User-Agent": "imf-rer-streamlit-app"}
START_PERIOD = "1976-M01"
EURO_AREA = "G163"
REQUEST_TIMEOUT = 90  # seconds
CACHE_TTL = 6 * 60 * 60  # 6 hours

# Euro-area members: irrevocable conversion rate (legacy units per EUR)
# and the first month the euro was the country's currency.
EURO_MEMBERS: dict[str, tuple[float, str]] = {
    "AUT": (13.7603, "1999-01"),
    "BEL": (40.3399, "1999-01"),
    "DEU": (1.95583, "1999-01"),
    "ESP": (166.386, "1999-01"),
    "FIN": (5.94573, "1999-01"),
    "FRA": (6.55957, "1999-01"),
    "IRL": (0.787564, "1999-01"),
    "ITA": (1936.27, "1999-01"),
    "LUX": (40.3399, "1999-01"),
    "NLD": (2.20371, "1999-01"),
    "PRT": (200.482, "1999-01"),
    "GRC": (340.750, "2001-01"),
    "SVN": (239.640, "2007-01"),
    "CYP": (0.585274, "2008-01"),
    "MLT": (0.429300, "2008-01"),
    "SVK": (30.1260, "2009-01"),
    "EST": (15.6466, "2011-01"),
    "LVA": (0.702804, "2014-01"),
    "LTU": (3.45280, "2015-01"),
    "HRV": (7.53450, "2023-01"),
    "BGR": (1.95583, "2026-01"),
}


# --------------------------------------------------------------------------
# IMF API helpers
# --------------------------------------------------------------------------
def _get_json(url: str, params: dict | None = None, retries: int = 3) -> dict:
    """GET a JSON document from the IMF API with simple retries."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"IMF API request failed: {url} ({last_err})")


def _period_to_month(value: str) -> pd.Period | None:
    """Convert an IMF time code such as '1976-M01' to a monthly Period."""
    try:
        if "-M" in value:
            year, month = value.split("-M")
            return pd.Period(year=int(year), month=int(month), freq="M")
        return pd.Period(value, freq="M")
    except (ValueError, TypeError):
        return None


def parse_sdmx_series(doc: dict) -> pd.Series:
    """Parse an SDMX-JSON 2.0 data message holding one series into a Series."""
    empty = pd.Series(dtype="float64")
    try:
        data = doc["data"]
        structure = data["structures"][0]
        time_values = structure["dimensions"]["observation"][0]["values"]
        series_map = data["dataSets"][0].get("series") or {}
    except (KeyError, IndexError, TypeError):
        return empty
    if not series_map:
        return empty

    observations = next(iter(series_map.values())).get("observations") or {}
    points: dict[pd.Period, float] = {}
    for idx, obs in observations.items():
        try:
            tv = time_values[int(idx)]
            period = _period_to_month(tv.get("value") or tv.get("id"))
            raw = obs[0] if obs else None
            if period is None or raw in (None, "", "NaN"):
                continue
            points[period] = float(raw)
        except (ValueError, IndexError, TypeError):
            continue

    if not points:
        return empty
    s = pd.Series(points, dtype="float64").sort_index()
    s.index = pd.PeriodIndex(s.index, freq="M")
    return s


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_series(dataflow: str, key: str) -> pd.Series:
    """Fetch one monthly series (from START_PERIOD onward) from the IMF API."""
    url = f"{IMF_BASE}/data/dataflow/{AGENCY}/{dataflow}/+/{key}"
    doc = _get_json(url, params={"c[TIME_PERIOD]": f"ge:{START_PERIOD}"})
    return parse_sdmx_series(doc)


def _series_country_codes(dataflow: str, key: str) -> set[str]:
    """Return the country codes that have a series matching a wildcard key."""
    url = f"{IMF_BASE}/data/dataflow/{AGENCY}/{dataflow}/+/{key}"
    doc = _get_json(url, params={"detail": "serieskeysonly"})
    try:
        data = doc["data"]
        dim_values = data["structures"][0]["dimensions"]["series"][0]["values"]
        keys = (data["dataSets"][0].get("series") or {}).keys()
        return {dim_values[int(k.split(":")[0])]["id"] for k in keys}
    except (KeyError, IndexError, TypeError, ValueError):
        return set()


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def load_country_list() -> pd.DataFrame:
    """Countries with monthly CPI and exchange-rate series, plus the Euro area."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_names = pool.submit(_get_json, f"{IMF_BASE}/structure/codelist/IMF/CL_COUNTRY/+")
        f_cpi = pool.submit(_series_country_codes, "CPI", "*.CPI._T.IX.M")
        f_er = pool.submit(_series_country_codes, "ER", "*.XDC_USD.PA_RT.M")
        names_doc, cpi_codes, er_codes = f_names.result(), f_cpi.result(), f_er.result()

    names: dict[str, str] = {}
    try:
        for code in names_doc["data"]["codelists"][0]["codes"]:
            names[code["id"]] = code.get("name") or code["id"]
    except (KeyError, IndexError, TypeError):
        pass

    if cpi_codes and er_codes:
        codes = (cpi_codes & er_codes) | {EURO_AREA, "USA"}
    else:  # fallback: every 3-letter code in the IMF country list
        codes = {c for c in names if len(c) == 3 and c.isalpha()} | {EURO_AREA}

    rows = [{"code": c, "name": names.get(c, c)} for c in codes]
    df = pd.DataFrame(rows).sort_values("name", key=lambda s: s.str.lower())
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Country-level data
# --------------------------------------------------------------------------
def get_cpi(iso: str) -> tuple[pd.Series, str]:
    """Monthly CPI; falls back to HICP (used by the IMF for the Euro area)."""
    key = f"{iso}.CPI._T.IX.M"
    s = fetch_series("CPI", key)
    if s.empty:
        alt = f"{iso}.HICP._T.IX.M"
        s_alt = fetch_series("CPI", alt)
        if not s_alt.empty:
            return s_alt, alt
    return s, key


def get_exchange_rate(iso: str, splice_euro: bool) -> tuple[pd.Series, str]:
    """Local-currency units per USD (euro-equivalent for spliced euro members)."""
    key = f"{iso}.XDC_USD.PA_RT.M"
    s = fetch_series("ER", key)

    if s.empty and iso == "USA":
        idx = pd.period_range(START_PERIOD.replace("-M", "-"), pd.Timestamp.today(), freq="M")
        return pd.Series(1.0, index=idx), "USD per USD = 1"

    if splice_euro and iso in EURO_MEMBERS:
        conv, adopt = EURO_MEMBERS[iso]
        adopt_p = pd.Period(adopt, freq="M")
        euro = fetch_series("ER", f"{EURO_AREA}.XDC_USD.PA_RT.M")
        legacy = s[s.index < adopt_p] / conv
        after = euro[euro.index >= adopt_p]
        spliced = pd.concat([legacy, after]).sort_index()
        spliced = spliced[~spliced.index.duplicated(keep="last")]
        note = (
            f"{key} ÷ {conv:g} (legacy → EUR) before {adopt}, "
            f"then {EURO_AREA}.XDC_USD.PA_RT.M"
        )
        return spliced, note

    return s, key


def currency_label(iso: str, name: str, splice_euro: bool) -> str:
    if iso == EURO_AREA or (splice_euro and iso in EURO_MEMBERS):
        return "EUR"
    if iso == "USA":
        return "USD"
    return f"{name} currency"


def coverage(s: pd.Series) -> str:
    s = s.dropna()
    if s.empty:
        return "no data"
    return f"{s.index.min()} to {s.index.max()} ({len(s)} months)"


# --------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------
def build_frame(e1: pd.Series, e2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Align the four inputs on a monthly index and compute S(t) and RER(t)."""
    parts = [x for x in (e1, e2, p1, p2) if not x.empty]
    if not parts:
        return pd.DataFrame()
    start = pd.Period(START_PERIOD.replace("-M", "-"), freq="M")
    end = max(x.index.max() for x in parts)
    idx = pd.period_range(start, end, freq="M")
    df = pd.DataFrame(index=idx)
    df["E1"] = e1.reindex(idx)
    df["E2"] = e2.reindex(idx)
    df["P1"] = p1.reindex(idx)
    df["P2"] = p2.reindex(idx)
    df["S"] = df["E2"] / df["E1"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    return df


def window_stats(df: pd.DataFrame, start: pd.Period, end: pd.Period) -> tuple[pd.DataFrame, float | None]:
    """Slice to [start, end], compute RER* over valid months and Real Index."""
    w = df.loc[(df.index >= start) & (df.index <= end)].copy()
    valid = w["RER"].replace([np.inf, -np.inf], np.nan).dropna()
    valid = valid[valid > 0]
    rer_star = float(valid.mean()) if not valid.empty else None
    w["Valid"] = w.index.isin(valid.index)
    w["RER_star"] = rer_star
    w["Real_Index"] = 100 * w["RER"] / rer_star if rer_star else np.nan
    w.loc[~w["Valid"], "Real_Index"] = np.nan
    return w, rer_star


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="IMF Real Exchange Rate Explorer", page_icon="💱", layout="wide")
st.title("IMF Real Exchange Rate Explorer")
st.caption(
    "Monthly CPI and exchange-rate data from the IMF SDMX 3.0 API, January 1976 onward where available. "
    "S(t) = Country 2 currency per one unit of Country 1 currency; RER(t) = S(t) × P1(t) / P2(t)."
)

with st.spinner("Loading IMF country list (first load can take ~20 seconds)…"):
    try:
        countries = load_country_list()
    except RuntimeError as err:
        st.error(f"Could not load the IMF country list. {err}")
        st.stop()

codes = countries["code"].tolist()
labels = dict(zip(countries["code"], countries["name"]))


def _fmt(code: str) -> str:
    return f"{labels.get(code, code)} ({code})"


def _default(code: str, fallback: int) -> int:
    return codes.index(code) if code in codes else fallback


with st.sidebar:
    st.header("Countries")
    c1 = st.selectbox("Country 1", codes, index=_default("USA", 0), format_func=_fmt)
    c2 = st.selectbox("Country 2", codes, index=_default(EURO_AREA, min(1, len(codes) - 1)), format_func=_fmt)
    splice = st.checkbox(
        "Extend euro-member exchange rates with the euro",
        value=True,
        help=(
            "IMF exchange rates for euro-area members stop when they adopted the euro. "
            "When checked, legacy rates are converted to euro-equivalents at the official fixed "
            "conversion rate and continued with the Euro area (G163) rate."
        ),
    )

if c1 == c2:
    st.warning("Country 1 and Country 2 are the same, so S(t) = 1 and RER(t) = 1 throughout.")

n1, n2 = labels.get(c1, c1), labels.get(c2, c2)

with st.spinner(f"Fetching IMF data for {n1} and {n2}…"):
    try:
        e1, e1_key = get_exchange_rate(c1, splice)
        e2, e2_key = get_exchange_rate(c2, splice)
        p1, p1_key = get_cpi(c1)
        p2, p2_key = get_cpi(c2)
    except RuntimeError as err:
        st.error(f"IMF data request failed. Please try again shortly. {err}")
        st.stop()

df = build_frame(e1, e2, p1, p2)
valid_all = df["RER"].dropna() if not df.empty else pd.Series(dtype="float64")

with st.expander("Data sources and coverage", expanded=valid_all.empty):
    st.table(
        pd.DataFrame(
            {
                "Series": [f"E1: {n1} per USD", f"E2: {n2} per USD", f"P1: {n1} CPI", f"P2: {n2} CPI"],
                "IMF key": [e1_key, e2_key, p1_key, p2_key],
                "Coverage": [coverage(e1), coverage(e2), coverage(p1), coverage(p2)],
            }
        )
    )
    st.caption("IMF exchange rates are local-currency units per USD; the app converts them to S(t) = E2 / E1.")

if valid_all.empty:
    st.error(
        "No months have all four series (both exchange rates and both CPIs). "
        "The IMF may not publish monthly data for one of these countries. Try another pair."
    )
    st.stop()

# Sample-period selection (limited to months where any series exists)
months = list(df.index)
month_labels = [p.strftime("%Y-%m") for p in months]
first_valid, last_valid = valid_all.index.min(), valid_all.index.max()

with st.sidebar:
    st.header("Sample period")
    start_lbl, end_lbl = st.select_slider(
        "Start and end month",
        options=month_labels,
        value=(first_valid.strftime("%Y-%m"), last_valid.strftime("%Y-%m")),
    )
    st.caption(f"All four series overlap from {first_valid.strftime('%Y-%m')} to {last_valid.strftime('%Y-%m')}.")
    st.header("Chart")
    view = st.radio("Show", ["Real", "Nominal", "Both"], index=2, horizontal=True)

start_p, end_p = pd.Period(start_lbl, freq="M"), pd.Period(end_lbl, freq="M")
w, rer_star = window_stats(df, start_p, end_p)

cur1 = currency_label(c1, n1, splice)
cur2 = currency_label(c2, n2, splice)
s_units = f"{cur2} per {cur1}"

n_valid = int(w["Valid"].sum())
n_total = len(w)
m1, m2, m3 = st.columns(3)
m1.metric("RER* (window average)", f"{rer_star:,.4f}" if rer_star else "n/a")
m2.metric("Valid months", f"{n_valid} of {n_total}")
latest_s = w["S"].dropna()
m3.metric(f"Latest S(t) ({s_units})", f"{latest_s.iloc[-1]:,.4f}" if not latest_s.empty else "n/a",
          help=f"Month: {latest_s.index[-1]}" if not latest_s.empty else None)

if rer_star is None:
    st.error("No valid months in the selected window, so RER* cannot be computed. Widen the sample period.")
elif n_valid < n_total:
    st.info(f"{n_total - n_valid} month(s) in the window lack at least one series and are excluded from RER*; "
            "they appear as gaps in the chart and blanks in the CSV.")

# Chart
x = w.index.to_timestamp()
real_trace = go.Scatter(
    x=x, y=w["Real_Index"], name="Real Index (RER* = 100)", mode="lines",
    line=dict(color="#1f5fa8", width=2), connectgaps=False,
    hovertemplate="%{x|%Y-%m}<br>Real Index: %{y:.2f}<extra></extra>",
)
nom_trace = go.Scatter(
    x=x, y=w["S"], name=f"Nominal S(t) ({s_units})", mode="lines",
    line=dict(color="#d9822b", width=2), connectgaps=False,
    hovertemplate="%{x|%Y-%m}<br>S(t): %{y:,.4f}<extra></extra>",
)

if view == "Both":
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(real_trace, secondary_y=False)
    fig.add_trace(nom_trace, secondary_y=True)
    fig.update_yaxes(title_text="Real Index (RER* = 100)", secondary_y=False)
    fig.update_yaxes(title_text=f"S(t), {s_units}", secondary_y=True, showgrid=False)
elif view == "Real":
    fig = go.Figure(real_trace)
    fig.update_yaxes(title_text="Real Index (RER* = 100)")
else:
    fig = go.Figure(nom_trace)
    fig.update_yaxes(title_text=f"S(t), {s_units}")

if view in ("Real", "Both") and rer_star:
    fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.6)

fig.update_layout(
    title=f"{n1} (Country 1) vs {n2} (Country 2), {start_lbl} to {end_lbl}",
    hovermode="x unified", height=520, margin=dict(l=10, r=10, t=60, b=10),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0),
)
fig.update_xaxes(title_text="Month")
st.plotly_chart(fig, width="stretch")

st.caption(
    "A rising Real Index means Country 1's goods are becoming more expensive relative to Country 2's "
    "(real appreciation of Country 1's currency). CPI indices have different base years, so the RER level "
    "has no meaning on its own; the index normalizes it to the sample average."
)

# CSV download
out = pd.DataFrame(
    {
        "month": w.index.strftime("%Y-%m"),
        f"E1_{c1}_per_USD": w["E1"],
        f"E2_{c2}_per_USD": w["E2"],
        f"P1_CPI_{c1}": w["P1"],
        f"P2_CPI_{c2}": w["P2"],
        f"S_{c2}_per_{c1}": w["S"],
        "RER": w["RER"],
        "valid_month": w["Valid"],
        "RER_star": w["RER_star"],
        "Real_Index": w["Real_Index"],
    }
)
st.download_button(
    "Download CSV (all observations and calculations in the sample period)",
    data=out.to_csv(index=False).encode("utf-8"),
    file_name=f"rer_{c1}_{c2}_{start_lbl}_{end_lbl}.csv",
    mime="text/csv",
)

with st.expander("Show data table"):
    st.dataframe(out, width="stretch", hide_index=True)

st.caption("Source: International Monetary Fund, SDMX 3.0 API (IMF.STA CPI and ER dataflows).")
