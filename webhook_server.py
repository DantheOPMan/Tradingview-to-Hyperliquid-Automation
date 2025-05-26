import os, json
from fastapi import FastAPI, Request, HTTPException
import ccxt.async_support as ccxt

app = FastAPI()

TRADINGVIEW_SECRET = os.getenv("TRADINGVIEW_SECRET")
DEFAULT_SYMBOL     = os.getenv("SYMBOL", "BTC/USD")
LEVERAGE           = 5

# one CCXT client for all requests
exchange = ccxt.hyperliquid({
    "apiKey":          os.getenv("HYPE_API_KEY"),
    "secret":          os.getenv("HYPE_API_SECRET"),
    "enableRateLimit": True,
})

@app.post("/webhook")
async def handle_webhook(req: Request):
    data = await req.json()

    if data.get("secret") != TRADINGVIEW_SECRET:
        raise HTTPException(401, "Invalid secret")

    action = data.get("action", "").upper()
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    if action == "FLAT":
        positions = await exchange.fetch_positions()
        for pos in positions:
            if pos["symbol"] == symbol and pos["contractSize"] != 0:
                amt       = abs(pos["contractSize"])
                close_side = "sell" if pos["contractSize"] > 0 else "buy"
                order     = await exchange.create_order(
                    symbol,
                    "market",
                    close_side,
                    amt,
                    None,
                    {"leverage": LEVERAGE}
                )
                return {"status": "closed", "order": order}
        return {"status": "no_position"}

    # 3) BUY or SELL: build a full‐account, 5× side
    if action not in ("BUY", "SELL"):
        raise HTTPException(400, f"Unknown action: {action}")

    side = "buy" if action == "BUY" else "sell"

    # fetch free quote balance, e.g. USDT
    quote     = symbol.split("/")[1]
    balance   = await exchange.fetch_balance()
    available = balance["free"].get(quote, 0.0)
    if available <= 0:
        raise HTTPException(400, "Insufficient balance")

    # get current price
    ticker = await exchange.fetch_ticker(symbol)
    price  = ticker["last"]
    # compute base‐asset amount = (free quote * leverage) / price
    amount = (available * LEVERAGE) / price

    # 4) place market order with leverage param
    try:
        order = await exchange.create_order(
            symbol,
            "market",
            side,
            amount,
            None,
            {"leverage": LEVERAGE}
        )
    except Exception as e:
        raise HTTPException(500, f"Order failed: {e}")

    return {"status": "ok", "action": action, "order": order}
