"""
JARVIS Portfolio Tracker
─────────────────────────
Tracks personal holdings in a local SQLite DB.
Cross-references held tickers against AI Advisers signals (insiders, options, fund flows).

Actions exposed to JARVIS:
  add_holding(ticker, shares, cost_basis)
  remove_holding(ticker)
  get_portfolio_status()  → voice-friendly P&L + signal alerts
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH     = Path(__file__).parent / "data" / "portfolio.db"
AI_ADVISERS = (Path.home() / "Documents/AI-Advisers/memory")


def _init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            ticker      TEXT PRIMARY KEY,
            shares      REAL NOT NULL,
            cost_basis  REAL NOT NULL,
            added_date  TEXT,
            notes       TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def add_holding(ticker: str, shares: float, cost_basis: float, notes: str = "") -> str:
    """Add or update a position in the portfolio."""
    _init_db()
    ticker = ticker.upper().strip()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO holdings (ticker, shares, cost_basis, added_date, notes) VALUES (?,?,?,?,?)",
        (ticker, shares, cost_basis, datetime.now().strftime("%Y-%m-%d"), notes),
    )
    conn.commit()
    conn.close()
    return f"Added {shares:g} shares of {ticker} at ${cost_basis:.2f} cost basis, sir."


def remove_holding(ticker: str) -> str:
    """Remove a position from the portfolio."""
    _init_db()
    ticker = ticker.upper().strip()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT ticker FROM holdings WHERE ticker = ?", (ticker,)).fetchall()
    conn.execute("DELETE FROM holdings WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()
    if rows:
        return f"Removed {ticker} from your portfolio, sir."
    return f"{ticker} wasn't in your portfolio, sir."


def get_portfolio_status() -> str:
    """Return voice-friendly portfolio P&L with AI Advisers signal cross-references."""
    _init_db()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT ticker, shares, cost_basis FROM holdings").fetchall()
    conn.close()

    if not rows:
        return "Portfolio is empty, sir. Add positions with 'I bought X shares of Y at Z dollars'."

    lines = []
    total_value = 0.0
    total_cost  = 0.0
    fetch_errors = []

    for ticker, shares, cost_basis in rows:
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).fast_info
            price = float(info.last_price)
            value = price * shares
            cost  = cost_basis * shares
            pnl   = value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            total_value += value
            total_cost  += cost
            sign = "+" if pnl_pct >= 0 else ""
            lines.append(f"{ticker}: {shares:g}sh @ ${price:.2f} = ${value:,.0f} ({sign}{pnl_pct:.1f}%)")
        except Exception:
            lines.append(f"{ticker}: {shares:g} shares (price unavailable)")
            fetch_errors.append(ticker)

    total_pnl     = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    sign = "+" if total_pnl_pct >= 0 else ""

    summary = f"Portfolio: ${total_value:,.0f} total, {sign}{total_pnl_pct:.1f}% overall. "
    summary += " | ".join(lines)

    # ── AI Advisers signal cross-reference ──
    alerts = []
    held_tickers = {r[0] for r in rows}

    # Insider clusters on held tickers
    try:
        f = AI_ADVISERS / "insider_trading_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            for cluster in d.get("clusters", []):
                company = cluster.get("company", "")
                for t in held_tickers:
                    if t in company.upper():
                        action = "buying" if cluster.get("is_buy") else "selling"
                        alerts.append(f"Insider cluster {action} at {company}")
    except Exception:
        pass

    # Options tail risk
    try:
        f = AI_ADVISERS / "options_sentiment_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            if d.get("regime") in ("ELEVATED", "HIGH_STRESS", "FEAR"):
                alerts.append(f"Options regime: {d.get('regime')} — elevated downside hedging in market")
    except Exception:
        pass

    # Fund flow regime
    try:
        f = AI_ADVISERS / "fund_flow_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            regime = d.get("regime", "")
            if regime in ("RISK_OFF", "DEFENSIVE", "CREDIT_STRESS") or d.get("regime_changed"):
                changed = " — REGIME JUST CHANGED" if d.get("regime_changed") else ""
                alerts.append(f"Fund flows: {regime}{changed}")
    except Exception:
        pass

    # Insider overall sentiment
    try:
        f = AI_ADVISERS / "insider_trading_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            sentiment = d.get("buy_sell_ratio", {}).get("sentiment", "")
            if sentiment == "BEARISH":
                buys  = d.get("buy_sell_ratio", {}).get("buys", 0)
                sells = d.get("buy_sell_ratio", {}).get("sells", 0)
                alerts.append(f"Insiders broadly bearish: {buys} buys vs {sells} sells today")
    except Exception:
        pass

    if alerts:
        summary += "\n\nSIGNALS: " + " | ".join(alerts)

    return summary
