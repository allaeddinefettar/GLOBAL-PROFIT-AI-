import asyncio
import ccxt.async_support as ccxt
import json
import logging
import os
import time
from datetime import datetime, timezone

# ==========================================================
# GLOBAL PROFIT AI TITAN V4
# Kraken Spot | Paper Trading Only | Multi-timeframe scanner
# One scan per GitHub Actions run; schedule the workflow every 5 min.
# No API keys are required: this bot uses public market data only.
# ==========================================================

CONFIG = {
    "exchange": "kraken",
    "quote": "USDT",
    "entry_timeframe": "1m",
    "confirm_timeframes": ["5m", "15m"],
    "scan_interval": 300,
    "max_symbols": 25,
    "max_concurrent": 2,
    "refresh_symbols_seconds": 900,
    "starting_balance": 1000.0,
    "trade_amount": 25.0,
    "max_positions": 3,
    "fee_percent": 0.10,
    "slippage_percent": 0.05,
    "minimum_score": 4,
    "stop_atr": 1.5,
    "target_atr": 2.2,
    "max_daily_loss": 20.0,
    "minimum_quote_volume": 250_000,
    "minimum_atr_percent": 0.08,
    "maximum_atr_percent": 3.0,
    "paper_mode": True,
    "state_file": "titan_kraken_state.json",
    "trades_file": "titan_kraken_trades.jsonl",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("titan.log", encoding="utf-8"),
    ],
)

balance = CONFIG["starting_balance"]
positions = {}
daily_profit = 0.0
daily_date = datetime.now(timezone.utc).date().isoformat()
cached_symbols = []
last_symbol_refresh = 0.0
semaphore = asyncio.Semaphore(CONFIG["max_concurrent"])


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# -------------------------- STATE --------------------------

def save_state():
    state = {
        "version": 4,
        "exchange": CONFIG["exchange"],
        "balance": balance,
        "positions": positions,
        "daily_profit": daily_profit,
        "daily_date": daily_date,
    }
    tmp = CONFIG["state_file"] + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG["state_file"])
    except Exception:
        logging.exception("STATE SAVE ERROR")


def load_state():
    global balance, positions, daily_profit, daily_date
    path = CONFIG["state_file"]
    if not os.path.exists(path):
        logging.info("Starting a new Kraken V4 paper account")
        return

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    if state.get("exchange") not in (None, CONFIG["exchange"]):
        raise ValueError("Saved state belongs to a different exchange")

    loaded_balance = float(state["balance"])
    loaded_positions = state["positions"]
    loaded_daily_profit = float(state.get("daily_profit", 0.0))
    loaded_daily_date = state.get("daily_date", daily_date)

    if loaded_balance < 0 or not isinstance(loaded_positions, dict):
        raise ValueError("Invalid paper account state")

    for symbol, p in loaded_positions.items():
        required = (
            "entry", "quantity", "cost", "entry_fee", "stop", "target",
            "entry_candle", "last_checked_candle",
        )
        if not isinstance(p, dict) or any(k not in p for k in required):
            raise ValueError(f"Invalid saved position: {symbol}")

    balance = loaded_balance
    positions = loaded_positions
    daily_profit = loaded_daily_profit
    daily_date = loaded_daily_date
    logging.info(
        "Restored Kraken V4 paper account: cash %.2f, open positions %d",
        balance, len(positions)
    )


def reset_daily_stats():
    global daily_profit, daily_date
    today = datetime.now(timezone.utc).date().isoformat()
    if today != daily_date:
        daily_date = today
        daily_profit = 0.0
        save_state()
        logging.info("New UTC trading day")


# --------------------- INDICATORS -------------------------

def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((period - 1) * avg_gain + gains[i]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i - 1][4]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges[-period:]) / period


def timeframe_trend(candles):
    # Ignore the live, not-yet-closed candle.
    closed = candles[:-1]
    if len(closed) < 30:
        return None
    closes = [c[4] for c in closed]
    fast, slow = ema(closes, 9), ema(closes, 21)
    if fast is None or slow is None:
        return None
    return fast > slow and closes[-1] > fast


def analyze_entry(candles, trend_5m, trend_15m):
    if len(candles) < 40 or trend_5m is None or trend_15m is None:
        return None

    closed = candles[:-1]
    closes = [c[4] for c in closed]
    volumes = [c[5] for c in closed]
    price = closes[-1]
    fast, slow = ema(closes, 9), ema(closes, 21)
    strength = rsi(closes)
    volatility = atr(closed)

    if any(x is None for x in (fast, slow, strength, volatility)):
        return None
    if price <= 0 or volatility <= 0:
        return None

    avg_volume = sum(volumes[-21:-1]) / 20
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 0.0
    prior_high = max(c[2] for c in closed[-21:-1])
    atr_pct = (volatility / price) * 100.0

    score, reasons = 0, []
    if fast > slow and price > fast:
        score += 1
        reasons.append("1M UPTREND")
    if 50 <= strength <= 68:
        score += 1
        reasons.append("RSI MOMENTUM")
    if volume_ratio >= 1.3:
        score += 1
        reasons.append("VOLUME SURGE")
    if price > prior_high:
        score += 1
        reasons.append("BREAKOUT")
    if trend_5m:
        score += 1
        reasons.append("5M CONFIRM")
    if trend_15m:
        score += 1
        reasons.append("15M CONFIRM")

    if not (
        CONFIG["minimum_atr_percent"]
        <= atr_pct
        <= CONFIG["maximum_atr_percent"]
    ):
        return None

    return {
        "price": price,
        "atr": volatility,
        "atr_percent": atr_pct,
        "rsi": strength,
        "volume_ratio": volume_ratio,
        "score": score,
        "reasons": reasons,
        "candle_time": int(closed[-1][0]),
        "candle_high": float(closed[-1][2]),
        "candle_low": float(closed[-1][3]),
    }


# ---------------------- SYMBOLS ---------------------------

async def get_symbols(exchange):
    global cached_symbols, last_symbol_refresh
    now = time.time()
    if cached_symbols and now - last_symbol_refresh < CONFIG["refresh_symbols_seconds"]:
        return cached_symbols

    tickers = await exchange.fetch_tickers()
    candidates = []
    excluded = ("UP", "DOWN", "BULL", "BEAR")

    for symbol, market in (exchange.markets or {}).items():
        if market.get("spot") is not True or market.get("active") is False:
            continue
        if market.get("quote") != CONFIG["quote"]:
            continue

        base = market.get("base") or ""
        if any(base.endswith(suffix) for suffix in excluded):
            continue

        ticker = tickers.get(symbol) or {}
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            continue
        try:
            quote_volume = float(quote_volume)
        except (TypeError, ValueError):
            continue

        if quote_volume >= CONFIG["minimum_quote_volume"]:
            candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    cached_symbols = [s for s, _ in candidates[:CONFIG["max_symbols"]]]
    last_symbol_refresh = now

    logging.info(
        "Kraken: selected %d liquid %s spot pairs",
        len(cached_symbols), CONFIG["quote"]
    )
    if not cached_symbols:
        logging.warning(
            "No Kraken spot pairs passed the quote/volume filters. "
            "Check available USDT markets and volume threshold."
        )
    return cached_symbols


# ------------------- DATA COLLECTION ----------------------

async def fetch_candles(exchange, symbol, timeframe, limit=100):
    async with semaphore:
        return await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, limit=limit
        )


async def analyze_symbol(exchange, symbol):
    try:
        one, five, fifteen = await asyncio.gather(
            fetch_candles(exchange, symbol, "1m", 100),
            fetch_candles(exchange, symbol, "5m", 100),
            fetch_candles(exchange, symbol, "15m", 100),
        )

        trend5 = timeframe_trend(five)
        trend15 = timeframe_trend(fifteen)
        result = analyze_entry(one, trend5, trend15)

        # Keep closed 1m candles so position management can inspect
        # candles since the previous scheduled GitHub Actions run.
        closed = one[:-1]
        if len(closed) < 2:
            return None

        latest = closed[-1]
        if result is None:
            result = {
                "price": float(latest[4]),
                "atr": None,
                "candle_time": int(latest[0]),
                "candle_high": float(latest[2]),
                "candle_low": float(latest[3]),
                "score": 0,
                "rsi": 0.0,
                "volume_ratio": 0.0,
                "reasons": [],
            }

        result["closed_candles"] = [
            {
                "time": int(c[0]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
            }
            for c in closed
        ]
        result["symbol"] = symbol
        return result

    except Exception as exc:
        logging.warning("ANALYSIS ERROR %s: %s", symbol, exc)
        return None


# ----------------------- HISTORY --------------------------

def save_trade(record):
    try:
        with open(CONFIG["trades_file"], "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("TRADE HISTORY ERROR")


# -------------------- PAPER EXECUTION ---------------------

def open_paper_trade(signal):
    global balance

    symbol = signal["symbol"]
    market_price = signal["price"]
    volatility = signal["atr"]

    if symbol in positions or len(positions) >= CONFIG["max_positions"]:
        return False
    if market_price <= 0 or volatility is None or volatility <= 0:
        return False

    amount = min(
        CONFIG["trade_amount"],
        balance / (1 + CONFIG["fee_percent"] / 100)
    )
    if amount < 5:
        return False

    fee = amount * CONFIG["fee_percent"] / 100
    execution = market_price * (1 + CONFIG["slippage_percent"] / 100)
    quantity = amount / execution

    stop = execution - CONFIG["stop_atr"] * volatility
    target = execution + CONFIG["target_atr"] * volatility
    if stop <= 0 or target <= execution:
        return False

    positions[symbol] = {
        "entry": execution,
        "quantity": quantity,
        "cost": amount,
        "entry_fee": fee,
        "stop": stop,
        "target": target,
        "entry_candle": signal["candle_time"],
        "last_checked_candle": signal["candle_time"],
        "opened": utc_now(),
    }

    balance -= amount + fee
    save_state()

    logging.warning(
        "PAPER BUY %s | entry %.8f | score %d/6",
        symbol, execution, signal["score"]
    )
    return True


def close_paper_trade(symbol, market_price, reason):
    global balance, daily_profit

    position = positions.get(symbol)
    if not position:
        return

    execution = market_price * (1 - CONFIG["slippage_percent"] / 100)
    revenue = position["quantity"] * execution
    exit_fee = revenue * CONFIG["fee_percent"] / 100
    pnl = revenue - exit_fee - position["cost"] - position["entry_fee"]

    balance += revenue - exit_fee
    daily_profit += pnl

    save_trade({
        "exchange": "kraken",
        "symbol": symbol,
        "entry": position["entry"],
        "exit": execution,
        "quantity": position["quantity"],
        "profit_usdt": round(pnl, 6),
        "reason": reason,
        "time": utc_now(),
        "mode": "PAPER",
    })

    del positions[symbol]
    save_state()

    logging.warning(
        "PAPER SELL %s | %s | PNL %.5f USDT",
        symbol, reason, pnl
    )


# ------------------- POSITION CONTROL ---------------------

def check_positions(signals):
    by_symbol = {s["symbol"]: s for s in signals}
    candle_ms = 60_000

    for symbol in list(positions):
        signal = by_symbol.get(symbol)
        if not signal:
            logging.warning(
                "POSITION NOT CHECKED: no fresh candle data for %s", symbol
            )
            continue

        p = positions[symbol]
        candles = signal.get("closed_candles", [])

        if candles and p["last_checked_candle"] < candles[0]["time"] - candle_ms:
            logging.error(
                "CANDLE HISTORY GAP for %s: last checked %s, oldest available %s. "
                "Review this paper position manually.",
                symbol, p["last_checked_candle"], candles[0]["time"]
            )

        for candle in candles:
            candle_time = candle["time"]
            if candle_time <= p["entry_candle"]:
                continue
            if candle_time <= p["last_checked_candle"]:
                continue

            low, high = candle["low"], candle["high"]

            # Conservative assumption: stop is hit first if both levels
            # were touched inside the same 1m candle.
            if low <= p["stop"]:
                p["last_checked_candle"] = candle_time
                close_paper_trade(symbol, p["stop"], "STOP LOSS")
                break
            elif high >= p["target"]:
                p["last_checked_candle"] = candle_time
                close_paper_trade(symbol, p["target"], "TAKE PROFIT")
                break
            else:
                p["last_checked_candle"] = candle_time

    save_state()


def estimate_equity(signals):
    prices = {s["symbol"]: s["price"] for s in signals}
    equity = balance
    for symbol, p in positions.items():
        equity += p["quantity"] * prices.get(symbol, p["entry"])
    return equity


# ---------------------- MAIN SCAN -------------------------

async def scan_market(exchange):
    reset_daily_stats()

    symbols = await get_symbols(exchange)
    # Always include open positions even if they leave the liquid-pair shortlist.
    symbols = list(dict.fromkeys(symbols + list(positions.keys())))
    logging.info("Scanning %d Kraken pairs", len(symbols))

    if not symbols:
        logging.warning("No symbols available to scan; ending this run safely.")
        return

    results = await asyncio.gather(
        *(analyze_symbol(exchange, symbol) for symbol in symbols),
        return_exceptions=True,
    )
    signals = [x for x in results if isinstance(x, dict)]

    check_positions(signals)

    if daily_profit <= -CONFIG["max_daily_loss"]:
        logging.error("DAILY LOSS LIMIT reached; entries paused")
        save_state()
        return

    opportunities = sorted(
        (s for s in signals if s["score"] >= CONFIG["minimum_score"]),
        key=lambda x: (x["score"], x["volume_ratio"]),
        reverse=True,
    )

    for signal in opportunities:
        if len(positions) >= CONFIG["max_positions"]:
            break
        if signal["symbol"] in positions:
            continue

        logging.info(
            "SIGNAL %s | score %d/6 | RSI %.1f | volume x%.2f | %s",
            signal["symbol"], signal["score"], signal["rsi"],
            signal["volume_ratio"], ", ".join(signal["reasons"])
        )
        open_paper_trade(signal)

    logging.info(
        "CASH %.2f | EQUITY %.2f | REALIZED DAILY PNL %.2f | OPEN %d",
        balance, estimate_equity(signals), daily_profit, len(positions)
    )
    save_state()


async def main():
    if CONFIG["paper_mode"] is not True:
        raise RuntimeError("Live trading is intentionally disabled in V4.")

    load_state()

    exchange_class = getattr(ccxt, CONFIG["exchange"])
    exchange = exchange_class({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},
    })

    try:
        logging.info("Connecting to Kraken public market data...")
        await exchange.load_markets()

        usdt_spot_markets = [
            symbol
            for symbol, market in exchange.markets.items()
            if market.get("spot") is True
            and market.get("active") is not False
            and market.get("quote") == CONFIG["quote"]
        ]

        logging.info(
            "Kraken markets loaded: %d active %s spot pairs",
            len(usdt_spot_markets), CONFIG["quote"]
        )

        if not usdt_spot_markets:
            raise RuntimeError(
                "Kraken returned no active USDT spot markets. "
                "Check quote currency availability before proceeding."
            )

        logging.info("GLOBAL PROFIT AI TITAN V4 STARTED | PAPER ONLY")
        await scan_market(exchange)
        logging.info("SCAN COMPLETED")

    finally:
        save_state()
        await exchange.close()
        logging.info("TITAN V4 STOPPED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    except Exception:
        logging.exception("FATAL ERROR")
        raise
