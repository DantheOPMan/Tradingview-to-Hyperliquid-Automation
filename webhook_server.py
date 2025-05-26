from dotenv import load_dotenv
load_dotenv()

import os, json, sys
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import ccxt.async_support as ccxt
import httpx

app = FastAPI()

# Load our envs once
TRADINGVIEW_SECRET   = os.getenv("TRADINGVIEW_SECRET")
HYPE_API_KEY         = os.getenv("HYPE_API_KEY")
HYPE_API_SECRET      = os.getenv("HYPE_API_SECRET")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL")
WALLET_ADDRESS       = os.getenv("WALLET_ADDRESS")
DEFAULT_SYMBOL       = os.getenv("SYMBOL", "BTC/USDC:USDC")
LEVERAGE             = 5

# CCXT client — note the walletAddress param
exchange = ccxt.hyperliquid({
    "walletAddress":   WALLET_ADDRESS,
    "secret":          HYPE_API_SECRET,
    "enableRateLimit": True,
})

async def notify_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL not set; skipping Discord notification")
        return
    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": content})

@app.on_event("startup")
async def on_startup():
    # Validate env vars
    required = {
        "TRADINGVIEW_SECRET": TRADINGVIEW_SECRET,
        "HYPE_API_KEY":       HYPE_API_KEY,
        "HYPE_API_SECRET":    HYPE_API_SECRET,
        "DISCORD_WEBHOOK_URL":DISCORD_WEBHOOK_URL,
        "WALLET_ADDRESS":     WALLET_ADDRESS,
    }
    missing = [n for n,v in required.items() if not v]
    if missing:
        msg = f"🚨 Missing environment variables: {', '.join(missing)}"
        await notify_discord(msg)
        print(msg)
        sys.exit(1)
    await notify_discord("✅ All environment variables are set correctly! Service is live.")
    print("✅ Env check passed. Service is live.")

@app.on_event("shutdown")
async def on_shutdown():
    await notify_discord("🛑 Service is shutting down")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    msg = f"⚠️ ERROR: {exc}"
    await notify_discord(msg)
    return PlainTextResponse(str(exc), status_code=500)

@app.post("/webhook")
async def handle_webhook(req: Request):
    data = await req.json()
    if data.get("secret") != TRADINGVIEW_SECRET:
        raise HTTPException(401, "Invalid secret")

    action = data.get("action", "").upper()
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    # fetch price once for notifications
    ticker = await exchange.fetch_ticker(symbol)
    price  = ticker["last"]

    if action == "FLAT":
        positions = await exchange.fetch_positions()
        for pos in positions:
            if pos["symbol"] == symbol and pos["contractSize"] != 0:
                amt        = abs(pos["contractSize"])
                close_side = "sell" if pos["contractSize"] > 0 else "buy"
                order      = await exchange.create_order(
                    symbol, "market", close_side, amt, None, {"leverage": LEVERAGE}
                )
                await notify_discord(f"{symbol} FLAT {price:.2f}")
                return {"status": "closed", "order": order}
        await notify_discord(f"{symbol} FLAT {price:.2f}")
        return {"status": "no_position"}

    if action not in ("BUY", "SELL"):
        raise HTTPException(400, f"Unknown action: {action}")

    side = "buy" if action == "BUY" else "sell"
    quote     = symbol.split("/")[1]
    balance   = await exchange.fetch_balance()
    available = balance["free"].get(quote, 0.0)
    if available <= 0:
        # Notify Discord with the actual available balance
        msg = f"{symbol} {action} {price:.2f} — insufficient balance: {available:.6f} {quote}"
        await notify_discord(msg)
        # Return a 400 with the available amount in the detail
        raise HTTPException(400, f"Insufficient balance: {available:.6f} {quote}")

    amount = (available * LEVERAGE) / price

    try:
        order = await exchange.create_order(
            symbol, "market", side, amount, None, {"leverage": LEVERAGE}
        )
    except Exception as e:
        await notify_discord(f"{symbol} {action} {price:.2f}")
        raise HTTPException(500, f"Order failed: {e}")

    await notify_discord(f"{symbol} {action} {price:.2f}")
    return {"status": "ok", "action": action, "order": order}
