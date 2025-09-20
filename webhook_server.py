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
WALLET_ADDRESS      = os.getenv("WALLET_ADDRESS")
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
        async with httpx.AsyncClient(timeout=10.0) as client:
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
        await asyncio.sleep(3)
        sys.exit(1)

    logger.info("✅ All environment variables set! Service is live.")
    await notify_discord("✅ All environment variables set! Service is live.")

# ─── Shutdown Notification ───────────────────────────────────────────────────
@app.on_event("shutdown")
async def on_shutdown():
    try:
        await notify_discord("🛑 Service is shutting down")
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
        usdc_balance = resp.get("USDC", {})
        total = float(usdc_balance.get("total", 0.0))
        free  = float(usdc_balance.get("free", 0.0))
        used  = float(usdc_balance.get("used", 0.0))
        return {"total": total, "hold": used, "free": free}
    except (Exception, ValueError, TypeError) as e:
        logger.error(f"fetch_balance failed or returned invalid data: {e}")
        raise HTTPException(502, f"Balance fetch error: {e}")


# ─── Main Webhook Endpoint ───────────────────────────────────────────────────
@app.post("/webhook")
async def handle_webhook(request: Request):
    # 1) Parse & Validate JSON
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

    # 3) Fetch current positions FIRST to get a clear state
    current_size = 0.0
    try:
        positions = await exchange.fetch_positions([symbol])
        if positions:
            # Find the specific position for our symbol
            pos = next((p for p in positions if p.get("symbol") == symbol), None)
            if pos and pos.get('info', {}).get('position'):
                # Safely get the size ('szi')
                current_size = float(pos['info']['position'].get('szi', 0.0))
    except Exception as e:
        logger.error(f"Failed to fetch positions for {symbol}: {e}")
        await notify_discord(f"🚨 {symbol} FETCH_POSITIONS_FAILED: {e}")
        raise HTTPException(502, f"Position fetch error: {e}")

    # 4) Handle FLAT (Close any existing position)
    if action == "FLAT":
        if current_size != 0:
            try:
                side = "sell" if current_size > 0 else "buy"
                amount = abs(current_size)
                logger.info(f"Executing FLAT command for {symbol}. Side: {side}, Amount: {amount}")
                order = await exchange.create_order(
                    symbol, "market", side, amount, params={"reduceOnly": True}
                )
                await notify_discord(f"✅ {symbol} FLAT command executed.")
                return {"status": "closed", "order": order}
            except Exception as e:
                logger.error(f"Error on FLAT for {symbol}: {e}")
                await notify_discord(f"🚨 {symbol} FLAT_FAILED: {e}")
                # Do not re-raise; allow logic to continue if needed, but log it
        else:
            logger.info(f"Received FLAT for {symbol}, but no position was open.")
            await notify_discord(f"ℹ️ {symbol} FLAT (no active position).")
        return {"status": "no_position_to_flat"}

    # 5) Validate action for new entries
    if action not in ("BUY", "SELL"):
        logger.warning(f"Unknown action received: {action}")
        raise HTTPException(400, f"Unknown action: {action}")

    # 6) Proactively close any opposing position before entering a new one
    is_entering_long = action == "BUY"
    is_entering_short = action == "SELL"
    
    if (is_entering_long and current_size < 0) or (is_entering_short and current_size > 0):
        try:
            side = "buy" if current_size < 0 else "sell"
            amount = abs(current_size)
            logger.info(f"Closing opposing {('SHORT' if current_size < 0 else 'LONG')} position for {symbol}.")
            await exchange.create_order(
                symbol, "market", side, amount, params={"reduceOnly": True}
            )
            await notify_discord(f"✅ {symbol} Closed opposing position before new entry.")
            # CRITICAL: Wait for the exchange to process the close
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Failed to close opposing position for {symbol}: {e}")
            await notify_discord(f"🚨 {symbol} CLOSE_OPPOSING_FAILED: {e}")
            raise HTTPException(500, f"Closing opposing position failed: {e}")

    # 7) Fetch latest price
    try:
        ticker = await exchange.fetch_ticker(symbol)
        price = float(ticker.get("last", 0.0))
        if price <= 0:
            raise ValueError("Price is zero or invalid")
    except Exception as e:
        logger.error(f"Failed to fetch ticker for {symbol}: {e}")
        await notify_discord(f"🚨 {symbol} FETCH_TICKER_FAILED: {e}")
        raise HTTPException(502, f"Price fetch error: {e}")

    # 8) Check balance and compute size
    usdc = await get_perp_usdc()
    if usdc["free"] < 1.0: # Using a small buffer
        msg = f"{symbol} {action} {price:.2f} — Insufficient USDC: free={usdc['free']:.2f}"
        logger.warning(msg)
        raise HTTPException(400, msg)

    side = "buy" if action == "BUY" else "sell"
    amount = (usdc["free"] * LEVERAGE) / price

    # 9) Send order
    try:
        logger.info(f"Placing new order: {symbol} {action} {amount:.6f} @ {price:.2f}")
        order = await exchange.create_order(
            symbol, "market", side, amount, params={"leverage": LEVERAGE}
        )
    except Exception as e:
        logger.error(f"Order failed: {e}")
        await notify_discord(f"🚨 {symbol} {action} FAILED: {e}")
        raise HTTPException(500, f"Order failed: {e}")

    # 10) Success
    logger.info(f"Order placed successfully: {order}")
    await notify_discord(f"✅ {symbol} {action} order placed at ~{price:.2f}")
    return {"status": "ok", "action": action, "order": order}