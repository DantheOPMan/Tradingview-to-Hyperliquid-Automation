from dotenv import load_dotenv
load_dotenv()

import os
import sys
import logging
import asyncio
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import ccxt.async_support as ccxt
import httpx

# ─── Configuration & Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tradingbot")

# ─── Environment Variables ──────────────────────────────────────────────────
TRADINGVIEW_SECRET  = os.getenv("TRADINGVIEW_SECRET")
HYPE_API_SECRET     = os.getenv("HYPE_API_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
WALLET_ADDRESS      = os.getenv("WALLET_ADDRESS")           # your API-wallet address
DEFAULT_SYMBOL      = os.getenv("SYMBOL", "BTC/USDC:USDC")
LEVERAGE            = int(os.getenv("LEVERAGE", 5))

# ─── CCXT Hyperliquid Client ────────────────────────────────────────────────
exchange = ccxt.hyperliquid({
    "walletAddress": WALLET_ADDRESS,
    "privateKey":    HYPE_API_SECRET,
    "enableRateLimit": True,
})
 
# ─── Discord Notifier ───────────────────────────────────────────────────────
async def notify_discord(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("No DISCORD_WEBHOOK_URL; skipping notification")
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(DISCORD_WEBHOOK_URL, json={"content": content})
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")

# ─── Startup / Env Validation ────────────────────────────────────────────────
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    missing = [k for k in (
        ("TRADINGVIEW_SECRET", TRADINGVIEW_SECRET),
        ("HYPE_API_SECRET",    HYPE_API_SECRET),
        ("WALLET_ADDRESS",     WALLET_ADDRESS),
        ("DISCORD_WEBHOOK_URL",DISCORD_WEBHOOK_URL),
    ) if not k[1]]
    if missing:
        msg = f"🚨 Missing environment variables: {', '.join(k[0] for k in missing)}"
        logger.critical(msg)
        await notify_discord(msg)
        # Give Discord up to 3 seconds, then exit
        await asyncio.sleep(3)
        sys.exit(1)

    logger.info("✅ All environment variables set! Service is live.")
    await notify_discord("✅ All environment variables set! Service is live.")

# ─── Shutdown Notification ───────────────────────────────────────────────────
@app.on_event("shutdown")
async def on_shutdown():
    try:
        await notify_discord("🛑 Service is shutting down")
    except Exception:
        pass
    finally:
        try:
            await exchange.close()
        except Exception as e:
            logger.error(f"Error closing exchange connection: {e}")

# ─── Global Exception Handler ────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    await notify_discord(f"⚠️ ERROR: {exc}")
    return PlainTextResponse(str(exc), status_code=500)

# ─── Helper: Fetch Perpetual USDC Balances ──────────────────────────────────
async def get_perp_usdc() -> Dict[str, float]:
    try:
        resp = await exchange.fetch_balance()
    except Exception as e:
        logger.error(f"fetch_balance failed: {e}")
        raise HTTPException(502, f"Balance fetch error: {e}")

    # Try both CCXT styles safely
    total = resp.get("USDC", {}).get("total") or resp.get("total", {}).get("USDC")
    free  = resp.get("USDC", {}).get("free")  or resp.get("free",  {}).get("USDC")
    used  = resp.get("USDC", {}).get("used")  or resp.get("used",  {}).get("USDC")

    # Fallback to zeros if anything missing
    try:
        total = float(total or 0.0)
        free  = float(free  or 0.0)
        used  = float(used  or 0.0)
    except (ValueError, TypeError):
        logger.warning("Balance response contained non-numeric values; defaulting to 0")
        total = free = used = 0.0

    hold = used
    return {"total": total, "hold": hold, "free": free}

# ─── Main Webhook Endpoint ───────────────────────────────────────────────────
@app.post("/webhook")
async def handle_webhook(request: Request):
    # 1) Parse & validate JSON
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Payload is not a JSON object")
    except Exception as e:
        logger.warning(f"Invalid JSON payload: {e}")
        raise HTTPException(400, f"Invalid JSON payload: {e}")

    # 2) Auth
    if data.get("secret") != TRADINGVIEW_SECRET:
        logger.warning("Unauthorized webhook call")
        raise HTTPException(401, "Invalid secret")

    action = str(data.get("action", "")).strip().upper()
    symbol = str(data.get("symbol", DEFAULT_SYMBOL)).strip()
    logger.info(f"Received webhook: action={action}, symbol={symbol}")

    # 3) Fetch price safely
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price  = float(ticker.get("last") or 0.0)
    except Exception as e:
        logger.error(f"Failed to fetch ticker for {symbol}: {e}")
        await notify_discord(f"{symbol} FETCH_TICKER_FAILED: {e}")
        raise HTTPException(502, f"Price fetch error: {e}")

    # 4) Handle FLAT (This remains the same for explicit closes)
    if action == "FLAT":
        try:
            positions = await exchange.fetch_positions([symbol])
            if positions:
                for pos in positions:
                    if pos.get("symbol") != symbol:
                        continue
                    size = float(pos.get("info", {}).get("position", {}).get("szi") or 0)
                    if size == 0:
                        continue
                    amt = abs(size)
                    close_side = "sell" if size > 0 else "buy"
                    order = await exchange.create_order(
                        symbol, "market", close_side, amt, price,
                        {"leverage": LEVERAGE, "reduceOnly": True},
                    )
                    await notify_discord(f"{symbol} FLAT {price:.2f}")
                    return {"status": "closed", "order": order}
        except Exception as e:
            logger.error(f"Error closing position on {symbol}: {e}")
            await notify_discord(f"{symbol} FLAT_FAILED: {e}")
            raise HTTPException(500, f"Closing position failed: {e}")

        await notify_discord(f"{symbol} FLAT (no active position) {price:.2f}")
        return {"status": "no_position"}

    # 5) Validate action
    if action not in ("BUY", "SELL"):
        logger.warning(f"Unknown action received: {action}")
        raise HTTPException(400, f"Unknown action: {action}")

    # NEW: 6) Proactively close any opposing position before entering
    try:
        positions = await exchange.fetch_positions([symbol])
        if positions:
            for pos in positions:
                if pos.get("symbol") != symbol:
                    continue
                
                current_size = float(pos.get('info', {}).get('position', {}).get('szi', 0))
                
                # If we want to BUY but have a SHORT position, or want to SELL and have a LONG position
                if (action == "BUY" and current_size < 0) or (action == "SELL" and current_size > 0):
                    logger.info(f"Closing opposing position for {symbol} before new entry.")
                    amt = abs(current_size)
                    close_side = "buy" if current_size < 0 else "sell"
                    await exchange.create_order(
                        symbol, "market", close_side, amt, price,
                        {"leverage": LEVERAGE, "reduceOnly": True}
                    )
                    await notify_discord(f"{symbol} Closed opposing position at {price:.2f}")
                    # Give a moment for the exchange to process the close
                    await asyncio.sleep(2) 

    except Exception as e:
        logger.error(f"Failed to close opposing position for {symbol}: {e}")
        await notify_discord(f"{symbol} CLOSE_OPPOSING_FAILED: {e}")
        # Depending on your risk tolerance, you might want to stop here
        raise HTTPException(500, f"Closing opposing position failed: {e}")


    # 7) Check balance
    usdc = await get_perp_usdc()
    if usdc["free"] <= 0:
        msg = (
            f"{symbol} {action} {price:.2f} — "
            f"insufficient USDC: free={usdc['free']:.6f}, hold={usdc['hold']:.6f}"
        )
        logger.warning(msg)
        raise HTTPException(400, msg)

    # 8) Compute size & send order
    side   = "buy" if action == "BUY" else "sell"
    amount = (usdc["free"] * LEVERAGE) / price if price > 0 else 0

    try:
        order = await exchange.create_order(
            symbol, "market", side, amount, price, {"leverage": LEVERAGE}
        )
    except Exception as e:
        logger.error(f"Order failed: {e}")
        await notify_discord(f"{symbol} {action} {price:.2f} — FAILED: {e}")
        raise HTTPException(500, f"Order failed: {e}")

    # 9) Success
    logger.info(f"Order placed: {symbol} {action} {amount:.6f}@{price:.2f}")
    await notify_discord(f"{symbol} {action} {price:.2f}")
    return {"status": "ok", "action": action, "order": order}
