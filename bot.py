
import ccxt
import time
import json
import logging
from datetime import datetime, timezone

# ==========================================
# GLOBAL PROFIT AI - PRO
# Cross Exchange Arbitrage Intelligence
# ==========================================

CONFIG = {
    "exchanges": ["binance", "bybit"],
    "symbols": [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "BNB/USDT",
        "ADA/USDT",
        "DOGE/USDT",
        "LTC/USDT"
    ],
    "quote_budget": 100.0,
    "fee_percent": {
        "binance": 0.10,
        "bybit": 0.10
    },
    "minimum_profit_percent": 0.30,
    "minimum_profit_usdt": 0.10,
    "scan_interval": 60,
    "paper_mode": True
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("global_profit.log")
    ]
)

def create_exchanges():
    result = {}

    for name in CONFIG["exchanges"]:
        exchange_class = getattr(ccxt, name)
        exchange = exchange_class({
            "enableRateLimit": True,
            "timeout": 15000
        })
        exchange.load_markets()
        result[name] = exchange

    return result


def buy_from_asks(asks, budget):
    remaining = budget
    quantity = 0
    cost = 0

    for price, amount in asks:
        take = min(amount, remaining / price)
        quantity += take
        cost += take * price
        remaining -= take * price

        if remaining <= 0.00000001:
            break

    if remaining > 0.01 or quantity == 0:
        return None

    return quantity, cost / quantity, cost


def sell_into_bids(bids, quantity):
    remaining = quantity
    proceeds = 0

    for price, amount in bids:
        take = min(amount, remaining)
        proceeds += take * price
        remaining -= take

        if remaining <= 0.00000001:
            break

    if remaining > 0.00000001:
        return None

    return proceeds / quantity, proceeds


def analyze(symbol, buy_name, sell_name, exchanges):
    try:
        buy_book = exchanges[buy_name].fetch_order_book(
            symbol, limit=20
        )
        sell_book = exchanges[sell_name].fetch_order_book(
            symbol, limit=20
        )

        if not buy_book["asks"] or not sell_book["bids"]:
            return

        buy = buy_from_asks(
            buy_book["asks"],
            CONFIG["quote_budget"]
        )

        if buy is None:
            return

        quantity, buy_price, cost = buy

        sell = sell_into_bids(
            sell_book["bids"],
            quantity
        )

        if sell is None:
            return

        sell_price, revenue = sell

        buy_fee = (
            cost * CONFIG["fee_percent"][buy_name] / 100
        )
        sell_fee = (
            revenue * CONFIG["fee_percent"][sell_name] / 100
        )

        net_profit = revenue - cost - buy_fee - sell_fee
        net_percent = net_profit / cost * 100

        result = {
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "buy_exchange": buy_name,
            "sell_exchange": sell_name,
            "quantity": quantity,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "net_profit_usdt": round(net_profit, 5),
            "net_profit_percent": round(net_percent, 4),
            "mode": "PAPER_ONLY"
        }

        logging.info(json.dumps(result))

        if (
            net_profit >= CONFIG["minimum_profit_usdt"]
            and net_percent >= CONFIG["minimum_profit_percent"]
        ):
            logging.warning(
                "POTENTIAL OPPORTUNITY: %s | BUY %s | SELL %s | "
                "NET %.4f USDT (%.3f%%)",
                symbol, buy_name, sell_name,
                net_profit, net_percent
            )

            with open("opportunities.jsonl", "a") as file:
                file.write(json.dumps(result) + "\n")

    except Exception as error:
        logging.error(
            "%s | %s -> %s | %s",
            symbol, buy_name, sell_name, error
        )


def scan_market(exchanges):
    logging.info("Starting GLOBAL PROFIT AI scan")

    for symbol in CONFIG["symbols"]:
        for buy_name in exchanges:
            for sell_name in exchanges:
                if buy_name == sell_name:
                    continue

                analyze(
                    symbol,
                    buy_name,
                    sell_name,
                    exchanges
                )


def main():
    logging.info("GLOBAL PROFIT AI PRO STARTING")

    if not CONFIG["paper_mode"]:
        raise RuntimeError(
            "Real trading is disabled in this version."
        )

    try:
        exchanges = create_exchanges()

        while True:
            start = time.time()

            scan_market(exchanges)

            elapsed = time.time() - start
            wait = max(1, CONFIG["scan_interval"] - elapsed)

            logging.info(
                "Scan complete. Next scan in %.1f seconds",
                wait
            )

            time.sleep(wait)

    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")

    except Exception:
        logging.exception("Fatal bot error")
        raise


if __name__ == "__main__":
    main()
