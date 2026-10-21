"""
JARVIS Market Data — real-time stock & crypto prices.

Uses Yahoo Finance (no key needed) for stocks and CoinGecko (free tier) for crypto.
"""

import asyncio
import logging
from typing import Optional

import httpx

log = logging.getLogger("jarvis.market")

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "MATIC", "AVAX", "LINK", "UNI", "DOT", "LTC"}
CRYPTO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "XRP": "ripple",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "DOT": "polkadot",
    "LTC": "litecoin",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0)"}


async def get_stock_price(symbol: str) -> Optional[dict]:
    """Fetch latest stock/ETF price from Yahoo Finance."""
    symbol = symbol.upper()
    try:
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            resp = await client.get(
                YAHOO_CHART.format(symbol=symbol),
                params={"interval": "1d", "range": "2d"},
            )
            if resp.status_code != 200:
                log.warning(f"Yahoo Finance {symbol}: HTTP {resp.status_code}")
                return None
            data = resp.json()
            result = data.get("chart", {}).get("result")
            if not result:
                return None
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            change = (price - prev) if (price and prev) else None
            change_pct = (change / prev * 100) if change and prev else None
            return {
                "symbol": symbol,
                "price": price,
                "prev_close": prev,
                "change": change,
                "change_pct": change_pct,
                "currency": meta.get("currency", "USD"),
                "exchange": meta.get("exchangeName", ""),
                "name": meta.get("longName") or meta.get("shortName") or symbol,
            }
    except Exception as e:
        log.warning(f"get_stock_price({symbol}) failed: {e}")
        return None


async def get_crypto_price(symbol: str) -> Optional[dict]:
    """Fetch crypto price from CoinGecko free API."""
    symbol = symbol.upper()
    coin_id = CRYPTO_IDS.get(symbol)
    if not coin_id:
        log.warning(f"Unknown crypto symbol: {symbol}")
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                COINGECKO_URL,
                params={
                    "ids": coin_id,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json().get(coin_id, {})
            price = data.get("usd")
            change_pct = data.get("usd_24h_change")
            market_cap = data.get("usd_market_cap")
            return {
                "symbol": symbol,
                "price": price,
                "prev_close": None,
                "change": None,
                "change_pct": change_pct,
                "currency": "USD",
                "market_cap": market_cap,
                "name": symbol,
            }
    except Exception as e:
        log.warning(f"get_crypto_price({symbol}) failed: {e}")
        return None


async def get_price(symbol: str) -> Optional[dict]:
    """Auto-detect stock vs crypto and fetch price."""
    clean = symbol.upper().replace("$", "").replace("-USD", "")
    if clean in CRYPTO_SYMBOLS:
        return await get_crypto_price(clean)
    return await get_stock_price(clean)


async def get_market_summary() -> dict:
    """Quick snapshot: SPY, QQQ, VIX, BTC."""
    results = await asyncio.gather(
        get_stock_price("SPY"),
        get_stock_price("QQQ"),
        get_stock_price("^VIX"),
        get_crypto_price("BTC"),
        return_exceptions=True,
    )
    labels = ["SPY", "QQQ", "VIX", "BTC"]
    summary = {}
    for label, r in zip(labels, results):
        if isinstance(r, dict) and r:
            summary[label] = r
    return summary


def format_price_for_voice(data: Optional[dict]) -> str:
    if not data:
        return "Price data unavailable, sir."
    symbol = data.get("symbol", "?")
    price = data.get("price")
    change_pct = data.get("change_pct")

    if price is None:
        return f"No price data for {symbol}, sir."

    if price >= 1000:
        price_str = f"${price:,.0f}"
    elif price >= 1:
        price_str = f"${price:,.2f}"
    else:
        price_str = f"${price:.4f}"

    if change_pct is not None:
        direction = "up" if change_pct > 0 else "down"
        return f"{symbol} is at {price_str}, {direction} {abs(change_pct):.1f}% today, sir."
    return f"{symbol} is trading at {price_str}, sir."


def format_market_summary_for_voice(summary: dict) -> str:
    if not summary:
        return "Market data unavailable, sir."
    parts = []
    for symbol, data in summary.items():
        price = data.get("price")
        chg = data.get("change_pct")
        if price is None:
            continue
        if price >= 1000:
            p = f"${price:,.0f}"
        else:
            p = f"${price:,.2f}"
        if chg is not None:
            arrow = "+" if chg >= 0 else ""
            parts.append(f"{symbol} {p} ({arrow}{chg:.1f}%)")
        else:
            parts.append(f"{symbol} {p}")
    return " | ".join(parts) + ", sir." if parts else "No market data, sir."
