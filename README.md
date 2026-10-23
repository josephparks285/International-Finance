# International-Finance: IMF Real Exchange Rate Explorer

A Streamlit app that compares any two IMF countries (or the Euro area) using monthly IMF consumer price and exchange-rate data from January 1976 onward, where available.

## What it calculates

| Symbol | Definition |
|---|---|
| E1(t), E2(t) | IMF exchange rates, local-currency units per USD |
| S(t) | Units of Country 2 currency per one unit of Country 1 currency: **S(t) = E2(t) / E1(t)** |
| P1(t), P2(t) | Monthly CPI index for Country 1 and Country 2 |
| RER(t) | **S(t) × P1(t) / P2(t)** |
| RER* | Average of RER(t) over valid months in the selected start and end months |
| Real Index(t) | **100 × RER(t) / RER\*** |

A month is valid only when all four inputs (E1, E2, P1, P2) are available. RER* is recalculated whenever the countries or the sample period change. A rising Real Index means Country 1's goods are becoming more expensive relative to Country 2's (a real appreciation of Country 1's currency).

## Features

- Choose Country 1 and Country 2 from IMF countries with monthly CPI and exchange-rate data, plus the Euro area (IMF code `G163`).
- Pick start and end months; the default is the full period in which all four series overlap.
- Interactive Plotly chart with a **Real / Nominal / Both** toggle. Real shows the Real Index; Nominal shows S(t) in actual currency units (not normalized); Both uses separate y-axes.
- CSV download of every month in the selected period with E1, E2, P1, P2, S(t), RER(t), a valid-month flag, RER*, and the Real Index. Missing data stays blank.
- A coverage table showing which IMF series were used and the months each covers.

## Data source

IMF SDMX 3.0 API: `https://api.imf.org/external/sdmx/3.0`

| Series | Dataflow | Key |
|---|---|---|
| Monthly CPI | `IMF.STA/CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate (LCU per USD, period average) | `IMF.STA/ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Notes on IMF coverage:

- **Euro area CPI.** The IMF publishes Euro area prices as HICP, so for `G163` the app falls back to `G163.HICP._T.IX.M` (monthly from December 1999). The Euro area exchange rate starts in January 1999.
- **Euro-area members.** The IMF exchange-rate series for each member stops when it adopted the euro (for example, Germany ends December 1998). With the sidebar option *Extend euro-member exchange rates with the euro* (on by default), legacy rates are converted to euro-equivalents at the official fixed conversion rates and continued with the Euro area rate, so S(t) is in euros for the whole period. Turn it off to use only the IMF's original legacy-currency series.
- **Gaps.** Many countries have shorter or patchy monthly histories. Gaps show as breaks in the chart and blanks in the CSV, and are excluded from RER*.
- **Base years.** CPI indices have different base years, so the level of RER(t) has no meaning on its own; the Real Index normalizes it to the sample average.

## Run locally

Requires Python 3.12.

```bash
pip install -r requirements.txt
streamlit run app.py
```

The first load fetches the IMF country list (around 20 seconds); responses are cached afterward.

## Deploy on Streamlit Community Cloud

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with your GitHub account.
2. Choose **Create app > Yup, I have an app**.
3. Select this repository, branch `main`, and main file `app.py`.
4. Under **Advanced settings**, select Python 3.12 and leave Secrets empty.
5. Click **Deploy**.

## Files

- `app.py` - the Streamlit application
- `requirements.txt` - Python dependencies
- `README.md` - this file
