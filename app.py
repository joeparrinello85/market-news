import streamlit as st
import pandas as pd
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import matplotlib.pyplot as plt
import numpy as np

# ==============================================================================
# Page Configuration
# ==============================================================================
st.set_page_config(
    page_title="Financial Fundamentals & Earnings Dashboard",
    page_icon="📈",
    layout="wide"
)

st.title("📊 Multi-Company 8-Quarter Financial Dashboard")
st.caption("Complete 8-Quarter Financial Breakdown & Visual Comparison")

# ==============================================================================
# Helper Formatting Functions
# ==============================================================================
def fmt_curr(val):
    if pd.isna(val) or val is None:
        return "N/A"
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1e12:
        return f"{sign}${abs_val / 1e12:.2f}T"
    if abs_val >= 1e9:
        return f"{sign}${abs_val / 1e9:.2f}B"
    if abs_val >= 1e6:
        return f"{sign}${abs_val / 1e6:.2f}M"
    return f"{sign}${abs_val:,.2f}"

def fmt_pct(val):
    if pd.isna(val) or val is None:
        return "N/A"
    return f"{val * 100:+.2f}%" if val != 0 else "0.00%"

def fmt_num(val, decimals=2):
    if pd.isna(val) or val is None or val == "N/A":
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)

# ==============================================================================
# Resilient Complete Statement Scraper (Zero Token / 8+ Quarters)
# ==============================================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

@st.cache_data(ttl=3600)
def fetch_complete_quarterly_history(ticker: str, num_quarters: int = 8) -> list:
    ticker = ticker.upper().strip()
    
    # 1. Primary Source: StockAnalysis / Financials endpoint
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/?p=quarterly"
    records = []
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            if table:
                df = pd.read_html(str(table))[0]
                # First column is metric name
                df = df.set_index(df.columns[0])
                
                # Clean column headers (Dates)
                dates = [c for c in df.columns if c != "Current"][:num_quarters]
                dates = list(reversed(dates)) # Chronological order
                
                def parse_val(row_aliases, col):
                    for alias in row_aliases:
                        for idx in df.index:
                            if alias.lower() in str(idx).lower():
                                raw = str(df.loc[idx, col]).replace("$", "").replace(",", "").replace("%", "").strip()
                                try:
                                    # Stockanalysis values are in millions
                                    return float(raw) * 1e6
                                except ValueError:
                                    continue
                    return None

                for i, d in enumerate(dates):
                    rev = parse_val(["Revenue", "Sales"], d)
                    gp = parse_val(["Gross Profit"], d)
                    op = parse_val(["Operating Income"], d)
                    net = parse_val(["Net Income", "Net Income Common"], d)
                    
                    if gp is None and rev is not None and op is not None:
                        gp = rev # For companies without cost of goods
                        
                    prev_d = dates[i - 1] if i > 0 else None
                    prev_rev = parse_val(["Revenue", "Sales"], prev_d) if prev_d else None
                    prev_net = parse_val(["Net Income", "Net Income Common"], prev_d) if prev_d else None

                    rev_growth = ((rev - prev_rev) / abs(prev_rev)) if (rev and prev_rev) else None
                    net_growth = ((net - prev_net) / abs(prev_net)) if (net and prev_net) else None
                    gross_margin = (gp / rev) if (gp and rev) else None
                    op_margin = (op / rev) if (op and rev) else None
                    net_margin = (net / rev) if (net and rev) else None

                    records.append({
                        "quarter": str(d),
                        "revenue": rev,
                        "rev_growth_qoq": rev_growth,
                        "gross_profit": gp,
                        "gross_margin": gross_margin,
                        "operating_income": op,
                        "operating_margin": op_margin,
                        "ebitda": op,
                        "net_income": net,
                        "net_growth_qoq": net_growth,
                        "net_margin": net_margin,
                    })
    except Exception:
        pass

    # 2. Fallback to yfinance if scraping fails
    if not records:
        try:
            stock = yf.Ticker(ticker)
            inc = stock.quarterly_income_stmt
            if not inc.empty:
                cols = list(reversed(list(inc.columns)[:num_quarters]))
                for i, c in enumerate(cols):
                    q_str = c.strftime("%Y-%m-%d") if hasattr(c, "strftime") else str(c)
                    rev = float(inc.loc["Total Revenue", c]) if "Total Revenue" in inc.index and pd.notna(inc.loc["Total Revenue", c]) else None
                    gp = float(inc.loc["Gross Profit", c]) if "Gross Profit" in inc.index and pd.notna(inc.loc["Gross Profit", c]) else None
                    op = float(inc.loc["Operating Income", c]) if "Operating Income" in inc.index and pd.notna(inc.loc["Operating Income", c]) else None
                    net = float(inc.loc["Net Income", c]) if "Net Income" in inc.index and pd.notna(inc.loc["Net Income", c]) else None

                    records.append({
                        "quarter": q_str,
                        "revenue": rev,
                        "rev_growth_qoq": None,
                        "gross_profit": gp,
                        "gross_margin": (gp / rev) if (gp and rev) else None,
                        "operating_income": op,
                        "operating_margin": (op / rev) if (op and rev) else None,
                        "ebitda": op,
                        "net_income": net,
                        "net_growth_qoq": None,
                        "net_margin": (net / rev) if (net and rev) else None,
                    })
        except Exception:
            pass

    return records

@st.cache_data(ttl=3600)
def load_company_data(ticker_str: str, num_quarters: int = 8) -> dict:
    ticker_clean = ticker_str.strip().upper()
    stock = yf.Ticker(ticker_clean)
    
    try:
        info = stock.info or {}
    except Exception:
        info = {}

    history = fetch_complete_quarterly_history(ticker_clean, num_quarters=num_quarters)
    return {
        "ticker": ticker_clean,
        "name": info.get("shortName", ticker_clean),
        "sector": info.get("sector", "N/A"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "market_cap": info.get("marketCap"),
        "history": history
    }

# ==============================================================================
# Sidebar Controls
# ==============================================================================
with st.sidebar:
    st.header("⚙️ Configuration")
    user_tickers = st.text_input("Stock Tickers (space or comma-separated)", value="AAPL MSFT NVDA")
    num_q = st.slider("Quarterly Depth", min_value=4, max_value=12, value=8, step=1)
    price_period = st.selectbox("Price Chart Period", options=["6mo", "1y", "2y", "5y"], index=1)
    st.info("Fetches complete 8-quarter income statements without missing line items.")

# ==============================================================================
# Main Dashboard
# ==============================================================================
tickers = sorted(list(set([t.strip().upper() for t in user_tickers.replace(",", " ").split() if t.strip()])))

if tickers:
    with st.spinner("Fetching full financial statements..."):
        results = [load_company_data(t, num_quarters=num_q) for t in tickers]

    # 1. Valuation Metrics
    st.subheader("1. Valuation & Overview")
    val_cols = st.columns(len(results))
    for idx, r in enumerate(results):
        with val_cols[idx]:
            st.metric(label=f"{r['name']} ({r['ticker']})", value=fmt_curr(r["market_cap"]), delta=f"Fwd P/E: {fmt_num(r['forward_pe'])}")
            st.caption(f"**Sector:** {r['sector']} | **Trailing P/E:** {fmt_num(r['trailing_pe'])}")

    st.divider()

    # 2. Price Performance Chart
    st.subheader(f"2. Normalized Stock Price Return ({price_period})")
    fig_price, ax_price = plt.subplots(figsize=(10, 3.5), dpi=200)
    has_price_data = False
    
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period=price_period)
            if not hist.empty and "Close" in hist.columns:
                close = hist["Close"]
                norm_returns = ((close - close.iloc[0]) / close.iloc[0]) * 100
                ax_price.plot(norm_returns.index, norm_returns.values, label=f"{t} ({norm_returns.values[-1]:+.1f}%)", linewidth=2)
                has_price_data = True
        except Exception:
            pass

    if has_price_data:
        ax_price.axhline(0, color="gray", linestyle="--", alpha=0.5)
        ax_price.set_ylabel("Return (%)")
        ax_price.legend(loc="upper left")
        fig_price.tight_layout()
        st.pyplot(fig_price)

    st.divider()

    # 3. Fundamentals Comparison
    st.subheader(f"3. {num_q}-Quarter Financial Fundamentals Comparison")
    valid_hist = [r for r in results if r.get("history")]
    
    if valid_hist:
        best_ticker = max(valid_hist, key=lambda x: len(x["history"]))
        quarters = [h["quarter"] for h in best_ticker["history"]]
        x = np.arange(len(quarters))
        num_comp = len(valid_hist)
        bar_width = 0.8 / max(num_comp, 1)

        fig_grid, axes = plt.subplots(2, 2, figsize=(12, 7), dpi=200)
        ax_rev, ax_ebitda = axes[0, 0], axes[0, 1]
        ax_net, ax_margin = axes[1, 0], axes[1, 1]

        for i, comp in enumerate(valid_hist):
            hist = comp["history"]
            h_map = {h["quarter"]: h for h in hist}
            
            rev_vals = [(h_map[q]["revenue"] / 1e9) if (q in h_map and h_map[q]["revenue"]) else 0 for q in quarters]
            ebitda_vals = [(h_map[q]["operating_income"] / 1e9) if (q in h_map and h_map[q]["operating_income"]) else 0 for q in quarters]
            net_vals = [(h_map[q]["net_income"] / 1e9) if (q in h_map and h_map[q]["net_income"]) else 0 for q in quarters]
            margin_vals = [(h_map[q]["gross_margin"] * 100) if (q in h_map and h_map[q]["gross_margin"]) else 0 for q in quarters]

            offset = (i - (num_comp - 1) / 2) * bar_width
            comp_x = x + offset

            ax_rev.bar(comp_x, rev_vals, width=bar_width, label=comp["ticker"], alpha=0.85)
            ax_ebitda.bar(comp_x, ebitda_vals, width=bar_width, label=comp["ticker"], alpha=0.85)
            ax_net.bar(comp_x, net_vals, width=bar_width, label=comp["ticker"], alpha=0.85)
            ax_margin.plot(x, margin_vals, marker='o', linewidth=2, label=comp["ticker"])

        ax_rev.set_title("Quarterly Revenue ($B)", fontweight="bold", fontsize=10)
        ax_rev.set_xticks(x)
        ax_rev.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
        ax_rev.legend()

        ax_ebitda.set_title("Quarterly Operating Income / EBITDA ($B)", fontweight="bold", fontsize=10)
        ax_ebitda.set_xticks(x)
        ax_ebitda.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
        ax_ebitda.legend()

        ax_net.set_title("Quarterly Net Income ($B)", fontweight="bold", fontsize=10)
        ax_net.set_xticks(x)
        ax_net.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
        ax_net.legend()

        ax_margin.set_title("Gross Margin Trend (%)", fontweight="bold", fontsize=10)
        ax_margin.set_xticks(x)
        ax_margin.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
        ax_margin.legend()

        fig_grid.tight_layout()
        st.pyplot(fig_grid)

    st.divider()

    # 4. Detailed Tables
    st.subheader("4. Detailed Financial Statement Tables")
    for r in results:
        hist = r.get("history", [])
        if hist:
            st.write(f"#### {r['name']} ({r['ticker']})")
            table_dict = {
                "Metric": [
                    "Total Revenue", "Revenue Growth (QoQ)", "Gross Profit", "Gross Margin %",
                    "Operating Income", "Operating Margin %", "Net Income", "Net Income Growth (QoQ)"
                ]
            }
            for h in hist:
                table_dict[h["quarter"]] = [
                    fmt_curr(h["revenue"]),
                    fmt_pct(h["rev_growth_qoq"]),
                    fmt_curr(h["gross_profit"]),
                    fmt_pct(h["gross_margin"]),
                    fmt_curr(h["operating_income"]),
                    fmt_pct(h["operating_margin"]),
                    fmt_curr(h["net_income"]),
                    fmt_pct(h["net_growth_qoq"]),
                ]
            df_table = pd.DataFrame(table_dict).set_index("Metric")
            st.dataframe(df_table, use_container_width=True)