from dotenv import load_dotenv
load_dotenv()

import os
import sys
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import ccxt.async_support as ccxt
import httpx

app = FastAPI()

# ─── Environment Variables ────────────────────────────────────────────────────
TRADINGVIEW_SECRET  = os.getenv("TRADINGVIEW_SECRET")
HYPE_API_SECRET     = os.getenv("HYPE_API_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
WALLET_ADDRESS      = os.getenv("WALLET_ADDRESS")           # your API‑wallet address
DEFAULT_SYMBOL      = os.getenv("SYMBOL", "BTC/USDC:USDC")
LEVERAGE            = int(os.getenv("LEVERAGE", 5))

# ─── CCXT Hyperliquid Client ─────────────────────────────────────────────────
exchange = ccxt.hyperliquid({
    "walletAddress": WALLET_ADDRESS,
    "privateKey":          HYPE_API_SECRET,
    "enableRateLimit": True,
})

# ─── Discord Notifier ─────────────────────────────────────────────────────────
async def notify_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  No DISCORD_WEBHOOK_URL; skipping notification")
        return
    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": content})

# ─── Startup / Env Validation ─────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    required = {
        "TRADINGVIEW_SECRET": TRADINGVIEW_SECRET,
        "HYPE_API_SECRET":    HYPE_API_SECRET,
        "WALLET_ADDRESS":     WALLET_ADDRESS,
        "DISCORD_WEBHOOK_URL":DISCORD_WEBHOOK_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        msg = f"🚨 Missing environment variables: {', '.join(missing)}"
        await notify_discord(msg)
        print(msg)
        sys.exit(1)
    msg = "✅ All environment variables set! Service is live."
    await notify_discord(msg)
    print(msg)

# ─── Shutdown Notification ────────────────────────────────────────────────────
@app.on_event("shutdown")
async def on_shutdown():
    await notify_discord("🛑 Service is shutting down")
    await exchange.close()

# ─── Global Exception Handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    msg = f"⚠️ ERROR: {exc}"
    await notify_discord(msg)
    return PlainTextResponse(str(exc), status_code=500)

# ─── Helper: Fetch Perpetual USDC Balances ────────────────────────────────────
async def get_perp_usdc():
    resp = await exchange.fetch_balance()
    balance = resp.get("USDC")
    if balance:
        total = float(balance.get("total", 0.0))
        free  = float(balance.get("free",  0.0))
        hold  = float(balance.get("used",  total - free))
        return {"total": total, "hold": hold, "free": free}

    free  = float(resp.get("free",  {}).get("USDC", 0.0))
    used  = float(resp.get("used",  {}).get("USDC", 0.0))
    total = float(resp.get("total", {}).get("USDC", free + used))
    return {"total": total, "hold": used, "free": free}


# ─── Main Webhook Endpoint ───────────────────────────────────────────────────
@app.post("/webhook")
async def handle_webhook(req: Request):
    data = await req.json()

    if data.get("secret") != TRADINGVIEW_SECRET:
        raise HTTPException(401, "Invalid secret")

    action = data.get("action", "").upper()
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    ticker = await exchange.fetch_ticker(symbol)
    price  = ticker["last"]

    if action == "FLAT":
        positions = await exchange.fetch_positions()
        for pos in positions:
            if pos["symbol"] == symbol:
                size_str = pos["info"]["position"]["szi"]
                size = float(size_str)
                if size == 0:
                    continue
                amt= abs(size)
                close_side = "sell" if size > 0 else "buy"
                order = await exchange.create_order(symbol,"market",close_side,amt,price,{"leverage": LEVERAGE,"reduceOnly": True,})
                await notify_discord(f"{symbol} FLAT {price:.2f}")
                return {"status": "closed", "order": order}
        await notify_discord(f"{symbol} FLAT {price:.2f}")
        return {"status": "no_position"}

    # 4) Validate action
    if action not in ("BUY", "SELL"):
        raise HTTPException(400, f"Unknown action: {action}")

    # 5) Check free USDC for new trade
    usdc = await get_perp_usdc()
    if usdc["free"] <= 0:
        msg = (
            f"{symbol} {action} {price:.2f} — "
            f"insufficient USDC: free={usdc['free']:.6f}, hold={usdc['hold']:.6f}"
        )
        #await notify_discord(msg)
        raise HTTPException(400, f"Insufficient balance: {usdc['free']:.6f} USDC")

    # 6) Compute position size (5× leverage)
    side   = "buy" if action == "BUY" else "sell"
    amount = (usdc["free"] * LEVERAGE) / price

    # 7) Place market order
    try:
        order = await exchange.create_order(
            symbol, "market", side, amount, price, {"leverage": LEVERAGE}
        )
    except Exception as e:
        await notify_discord(f"{symbol} {action} {price:.2f} — failed: {e}")
        raise HTTPException(500, f"Order failed: {e}")

    # 8) Success notification
    await notify_discord(f"{symbol} {action} {price:.2f}")
    return {"status": "ok", "action": action, "order": order}
