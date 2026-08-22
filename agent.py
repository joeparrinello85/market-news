import os
import sys
import argparse
from datetime import datetime
import requests
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import numpy as np

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
# SEC EDGAR Financials Fetcher (Fetches 8+ Quarters, 0 Tokens)
# ==============================================================================

SEC_HEADERS = {"User-Agent": "FinancialResearchTool contact@example.com"}

def get_sec_cik(ticker: str) -> str:
    """Finds CIK 10-digit code for SEC EDGAR API."""
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=10)
        data = res.json()
        for item in data.values():
            if item["ticker"].upper() == ticker.upper():
                return str(item["cik_str"]).zfill(10)
    except Exception:
        pass
    return None

def fetch_sec_quarterly_data(ticker: str, num_quarters: int = 8) -> list:
    """Fetches quarterly financial records directly from SEC EDGAR XBRL data."""
    cik = get_sec_cik(ticker)
    if not cik:
        return []

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        res = requests.get(url, headers=SEC_HEADERS, timeout=10)
        if res.status_code != 200:
            return []
        data = res.json()
    except Exception:
        return []

    gaap = data.get("facts", {}).get("us-gaap", {})
    if not gaap:
        return []

    def get_metric_series(concepts):
        for c in concepts:
            if c in gaap and "units" in gaap[c]:
                unit_key = list(gaap[c]["units"].keys())[0]
                rows = gaap[c]["units"][unit_key]
                # Filter for quarterly 10-Q forms with distinct 3-month durations
                q_rows = [r for r in rows if r.get("form") in ["10-Q", "10-K"] and "frame" in r and "Q" in r["frame"]]
                if q_rows:
                    return {r["frame"]: r["val"] for r in q_rows}
        return {}

    rev_map = get_metric_series(["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"])
    net_map = get_metric_series(["NetIncomeLoss", "ProfitLoss"])
    gp_map = get_metric_series(["GrossProfit"])
    op_map = get_metric_series(["OperatingIncomeLoss"])

    # Collect frames (e.g. CY2024Q1, CY2024Q2)
    all_frames = sorted(list(set(list(rev_map.keys()) + list(net_map.keys()))))[-num_quarters:]
    
    records = []
    for i, f in enumerate(all_frames):
        rev = rev_map.get(f)
        net = net_map.get(f)
        gp = gp_map.get(f)
        op = op_map.get(f)
        
        # Calculate derived metrics
        gross_margin = (gp / rev) if (gp and rev) else None
        op_margin = (op / rev) if (op and rev) else None
        net_margin = (net / rev) if (net and rev) else None
        
        prev_f = all_frames[i - 1] if i > 0 else None
        prev_rev = rev_map.get(prev_f) if prev_f else None
        prev_net = net_map.get(prev_f) if prev_f else None

        rev_growth_qoq = ((rev - prev_rev) / abs(prev_rev)) if (rev and prev_rev) else None
        net_growth_qoq = ((net - prev_net) / abs(prev_net)) if (net and prev_net) else None

        records.append({
            "quarter": f.replace("CY", ""),
            "revenue": rev,
            "rev_growth_qoq": rev_growth_qoq,
            "gross_profit": gp,
            "gross_margin": gross_margin,
            "operating_income": op,
            "operating_margin": op_margin,
            "ebitda": op, # Proxy EBITDA
            "net_income": net,
            "net_growth_qoq": net_growth_qoq,
            "net_margin": net_margin,
            "eps": None,
            "fcf": None
        })

    return records

# ==============================================================================
# Chart Generator 1: 1-Year Stock Price Return
# ==============================================================================

def generate_price_chart(tickers: list, chart_filename: str = "price_chart.png") -> str:
    try:
        plt.style.use("seaborn-v0_8-darkgrid" if "seaborn-v0_8-darkgrid" in plt.style.available else "default")
    except Exception:
        pass

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=300)
    has_plotted = False

    for t in tickers:
        try:
            stock = yf.Ticker(t)
            hist = stock.history(period="1y")
            if not hist.empty and "Close" in hist.columns:
                close = hist["Close"]
                norm_returns = ((close - close.iloc[0]) / close.iloc[0]) * 100
                ax.plot(norm_returns.index, norm_returns.values, label=f"{t} ({norm_returns.values[-1]:+.1f}%)", linewidth=2)
                has_plotted = True
        except Exception as e:
            print(f"\n  [Chart Error] Could not fetch price history for {t}: {e}")

    if not has_plotted:
        plt.close(fig)
        return ""

    ax.axhline(0, color="gray", linestyle="--", alpha=0.6, linewidth=1)
    ax.set_title("1-Year Comparative Price Performance (% Return)", fontsize=12, fontweight="bold", pad=10)
    ax.set_ylabel("Total Return (%)", fontsize=10)
    ax.set_xlabel("Date", fontsize=10)
    ax.legend(loc="upper left", frameon=True)
    fig.autofmt_xdate()
    fig.tight_layout()

    fig.savefig(chart_filename)
    plt.close(fig)
    return chart_filename

# ==============================================================================
# Chart Generator 2: 8-Quarter Financial Fundamentals Dashboard
# ==============================================================================

def generate_financial_chart(results: list, chart_filename: str = "financial_trends.png") -> str:
    valid = [r for r in results if "error" not in r and r.get("history")]
    if not valid:
        return ""

    try:
        plt.style.use("seaborn-v0_8-darkgrid" if "seaborn-v0_8-darkgrid" in plt.style.available else "default")
    except Exception:
        pass

    fig, axes = plt.subplots(2, 2, figsize=(14, 9), dpi=300)
    ax_rev, ax_ebitda = axes[0, 0], axes[0, 1]
    ax_net, ax_margin = axes[1, 0], axes[1, 1]

    quarters = [h["quarter"] for h in valid[0]["history"]]
    x = np.arange(len(quarters))
    num_companies = len(valid)
    bar_width = 0.8 / max(num_companies, 1)

    for i, comp in enumerate(valid):
        ticker = comp["ticker"]
        hist = comp["history"]
        
        rev_vals = [h["revenue"] / 1e9 if h["revenue"] is not None else 0 for h in hist]
        ebitda_vals = [h["ebitda"] / 1e9 if h["ebitda"] is not None else 0 for h in hist]
        net_vals = [h["net_income"] / 1e9 if h["net_income"] is not None else 0 for h in hist]
        margin_vals = [h["gross_margin"] * 100 if h["gross_margin"] is not None else 0 for h in hist]

        offset = (i - (num_companies - 1) / 2) * bar_width
        comp_x = np.arange(len(hist)) + offset

        ax_rev.bar(comp_x, rev_vals, width=bar_width, label=ticker, alpha=0.85)
        ax_ebitda.bar(comp_x, ebitda_vals, width=bar_width, label=ticker, alpha=0.85)
        ax_net.bar(comp_x, net_vals, width=bar_width, label=ticker, alpha=0.85)
        ax_margin.plot(np.arange(len(hist)), margin_vals, marker='o', linewidth=2, label=f"{ticker} Margin")

    ax_rev.set_title("Quarterly Revenue ($B)", fontsize=11, fontweight="bold")
    ax_rev.set_xticks(x)
    ax_rev.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
    ax_rev.legend()

    ax_ebitda.set_title("Quarterly Operating Income / EBITDA ($B)", fontsize=11, fontweight="bold")
    ax_ebitda.set_xticks(x)
    ax_ebitda.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
    ax_ebitda.legend()

    ax_net.set_title("Quarterly Net Income ($B)", fontsize=11, fontweight="bold")
    ax_net.set_xticks(x)
    ax_net.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
    ax_net.legend()

    ax_margin.set_title("Gross Margin Trend (%)", fontsize=11, fontweight="bold")
    ax_margin.set_ylabel("Gross Margin %", fontsize=9)
    ax_margin.set_xticks(x)
    ax_margin.set_xticklabels(quarters, rotation=35, ha="right", fontsize=8)
    ax_margin.legend()

    fig.suptitle("8-Quarter Financial Trends Comparison", fontsize=14, fontweight="bold", y=0.99)
    fig.tight_layout()
    fig.savefig(chart_filename)
    plt.close(fig)
    return chart_filename

# ==============================================================================
# Full Company Extractor
# ==============================================================================

def analyze_company_8_quarters(ticker_str: str) -> dict:
    ticker_clean = ticker_str.strip().upper()
    stock = yf.Ticker(ticker_clean)
    
    try:
        info = stock.info or {}
    except Exception:
        info = {}

    # Attempt SEC EDGAR extraction first for true 8-quarter depth
    history = fetch_sec_quarterly_data(ticker_clean, num_quarters=8)
    
    # Fallback to yfinance if SEC fails
    if not history:
        try:
            income_stmt = stock.quarterly_income_stmt
            available_cols = list(income_stmt.columns)[:8]
            chronological_cols = list(reversed(available_cols))
            for i, col in enumerate(chronological_cols):
                q_label = col.strftime("%b %Y") if hasattr(col, "strftime") else str(col)[:7]
                rev = income_stmt.loc["Total Revenue", col] if "Total Revenue" in income_stmt.index else None
                net = income_stmt.loc["Net Income", col] if "Net Income" in income_stmt.index else None
                history.append({
                    "quarter": q_label,
                    "revenue": float(rev) if rev is not None else None,
                    "rev_growth_qoq": None,
                    "gross_profit": None,
                    "gross_margin": None,
                    "operating_income": None,
                    "operating_margin": None,
                    "ebitda": None,
                    "net_income": float(net) if net is not None else None,
                    "net_growth_qoq": None,
                    "net_margin": None,
                    "eps": None,
                    "fcf": None
                })
        except Exception:
            pass

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
# Markdown Report Generator
# ==============================================================================

def generate_report(results: list, tickers: list, price_chart: str = "", fin_chart: str = "") -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    valid_results = [r for r in results if "error" not in r]
    
    if not valid_results:
        return "# Earnings Analysis Report\n\nNo valid financial data could be extracted."

    lines = []
    lines.append("# Multi-Company 8-Quarter Financial Analysis")
    lines.append(f"> **Generated on:** {timestamp} | **Coverage:** {', '.join(tickers)}\n")

    if price_chart and os.path.exists(price_chart):
        lines.append(f"### 1-Year Stock Price Return\n![Price Chart]({price_chart})\n")
    if fin_chart and os.path.exists(fin_chart):
        lines.append(f"### 8-Quarter Fundamentals (Revenue, Net Income, EBITDA & Margins)\n![Fundamentals Chart]({fin_chart})\n")

    lines.append("## Valuation & Market Overview\n")
    lines.append("| Metric | " + " | ".join([r["ticker"] for r in valid_results]) + " |")
    lines.append("| :--- | " + " | ".join([":---:" for _ in valid_results]) + " |")
    lines.append("| **Company** | " + " | ".join([r["name"] for r in valid_results]) + " |")
    lines.append("| **Sector** | " + " | ".join([r["sector"] for r in valid_results]) + " |")
    lines.append("| **Market Cap** | " + " | ".join([fmt_curr(r["market_cap"]) for r in valid_results]) + " |")
    lines.append("| **Trailing P/E** | " + " | ".join([fmt_num(r["trailing_pe"]) for r in valid_results]) + " |")
    lines.append("| **Forward P/E** | " + " | ".join([fmt_num(r["forward_pe"]) for r in valid_results]) + " |")

    lines.append("\n## Detailed 8-Quarter Financial Breakdown\n")
    for r in valid_results:
        hist = r["history"]
        if not hist:
            continue
        quarters = [h["quarter"] for h in hist]
        lines.append(f"### {r['name']} ({r['ticker']})\n")
        lines.append("| Category / Metric | " + " | ".join(quarters) + " |")
        lines.append("| :--- | " + " | ".join([":---:" for _ in quarters]) + " |")
        lines.append("| **Total Revenue** | " + " | ".join([fmt_curr(h["revenue"]) for h in hist]) + " |")
        lines.append("| **Revenue Growth (QoQ)** | " + " | ".join([fmt_pct(h["rev_growth_qoq"]) for h in hist]) + " |")
        lines.append("| **Gross Profit** | " + " | ".join([fmt_curr(h["gross_profit"]) for h in hist]) + " |")
        lines.append("| **Gross Margin %** | " + " | ".join([fmt_pct(h["gross_margin"]) for h in hist]) + " |")
        lines.append("| **Operating Income** | " + " | ".join([fmt_curr(h["operating_income"]) for h in hist]) + " |")
        lines.append("| **Operating Margin %** | " + " | ".join([fmt_pct(h["operating_margin"]) for h in hist]) + " |")
        lines.append("| **Net Income** | " + " | ".join([fmt_curr(h["net_income"]) for h in hist]) + " |")
        lines.append("| **Net Income Growth (QoQ)** | " + " | ".join([fmt_pct(h["net_growth_qoq"]) for h in hist]) + " |")
        lines.append("| **Net Profit Margin %** | " + " | ".join([fmt_pct(h["net_margin"]) for h in hist]) + " |")
        lines.append("\n")

    return "\n".join(lines)

# ==============================================================================
# CLI Orchestrator
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Financial Analysis & 8-Quarter Multi-Panel Charting Script")
    parser.add_argument("tickers", nargs="*", help="Stock ticker symbols (e.g. WMT COST TGT)")
    parser.add_argument("-o", "--output", help="Custom output filename for the markdown report")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(" 📊 Financial Scanner & 8-Quarter Multi-Panel Chart Generator")
    print("=" * 60)

    if args.tickers:
        raw_tickers = args.tickers
    else:
        user_in = input("\nEnter ticker symbols separated by space (e.g., WMT COST TGT): ").strip()
        if not user_in:
            print("No tickers provided. Exiting.")
            sys.exit(0)
        raw_tickers = user_in.replace(",", " ").split()

    tickers = sorted(list(set([t.strip().upper() for t in raw_tickers if t.strip()])))
    print(f"\nProcessing {len(tickers)} company/companies: {', '.join(tickers)}...")

    results = []
    for t in tickers:
        print(f"  --> Ingesting 8 quarters of data for {t}...", end="", flush=True)
        res = analyze_company_8_quarters(t)
        print(" [OK]")
        results.append(res)

    price_chart_file = f"{'_'.join(tickers)}_price_chart.png"
    print(f"  --> Plotting 1-year price performance ({price_chart_file})...", end="", flush=True)
    saved_price_chart = generate_price_chart(tickers, price_chart_file)
    print(" [OK]" if saved_price_chart else " [SKIPPED]")

    fin_chart_file = f"{'_'.join(tickers)}_fundamentals_chart.png"
    print(f"  --> Plotting full 8-quarter Revenue/Net/EBITDA/Margins ({fin_chart_file})...", end="", flush=True)
    saved_fin_chart = generate_financial_chart(results, fin_chart_file)
    print(" [OK]" if saved_fin_chart else " [SKIPPED]")

    report_md = generate_report(results, tickers, saved_price_chart, saved_fin_chart)
    out_name = args.output or f"{'_'.join(tickers)}_8Q_financial_report.md"

    with open(out_name, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"\n✅ Report written to: {os.path.abspath(out_name)}")
    if saved_price_chart:
        print(f"📈 Price chart:       {os.path.abspath(saved_price_chart)}")
    if saved_fin_chart:
        print(f"📊 Fundamentals chart: {os.path.abspath(saved_fin_chart)}\n")

if __name__ == "__main__":
    main()