from dotenv import load_dotenv
load_dotenv()

import os
import sys
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Dict, Set
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import ccxt.async_support as ccxt
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tradingbot")

TRADINGVIEW_SECRET  = os.getenv("TRADINGVIEW_SECRET")
HYPE_API_SECRET     = os.getenv("HYPE_API_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
WALLET_ADDRESS      = os.getenv("WALLET_ADDRESS")
DEFAULT_SYMBOL      = os.getenv("SYMBOL", "BTC/USDC:USDC")
LEVERAGE            = int(os.getenv("LEVERAGE", 5))

SIGNAL_BUFFER_SECONDS = 7 

exchange: Optional[ccxt.hyperliquid] = None
TRADE_LOCK = asyncio.Lock()

pending_actions: Dict[str, Set[str]] = defaultdict(set)
active_timers: Set[str] = set()

class WebhookPayload(BaseModel):
    secret: str
    action: str
    symbol: str = Field(default=DEFAULT_SYMBOL)
    leverage: Optional[int] = None

async def notify_discord(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(DISCORD_WEBHOOK_URL, json={"content": content})
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")

async def get_perp_usdc() -> Dict[str, float]:
    try:
        resp = await exchange.fetch_balance()
    except Exception as e:
        logger.error(f"fetch_balance failed: {e}")
        return {"total": 0.0, "hold": 0.0, "free": 0.0}

    total = resp.get("USDC", {}).get("total") or resp.get("total", {}).get("USDC")
    free  = resp.get("USDC", {}).get("free")  or resp.get("free",  {}).get("USDC")
    used  = resp.get("USDC", {}).get("used")  or resp.get("used",  {}).get("USDC")

    try:
        total = float(total or 0.0)
        free  = float(free  or 0.0)
        used  = float(used  or 0.0)
    except (ValueError, TypeError):
        total = free = used = 0.0

    return {"total": total, "hold": used, "free": free}

async def close_position(symbol: str, price: float) -> bool:
    try:
        positions = await exchange.fetch_positions()
        for pos in positions or []:
            if pos.get("symbol") != symbol:
                continue
            
            size = float(pos.get("info", {}).get("position", {}).get("szi") or 0)
            if size == 0:
                continue
            
            amt = abs(size)
            close_side = "sell" if size > 0 else "buy"
            
            logger.info(f"Closing position on {symbol}: size={size}, side={close_side}")
            await exchange.create_order(
                symbol, "market", close_side, amt, price,
                {"leverage": LEVERAGE, "reduceOnly": True},
            )
            await notify_discord(f"{symbol} CLOSED position at {price:.2f}")
            return True
            
    except Exception as e:
        logger.error(f"Error closing position on {symbol}: {e}")
        await notify_discord(f"{symbol} CLOSE_FAILED: {e}")
        return False
    
    return False

async def execute_trade_logic(symbol: str, action: str):
    action = action.upper()
    
    async with TRADE_LOCK:
        logger.info(f"Executing Decision for {symbol}: {action}")

        try:
            ticker = await exchange.fetch_ticker(symbol)
            price  = float(ticker.get("last") or 0.0)
        except Exception as e:
            logger.error(f"Failed to fetch ticker: {e}")
            await notify_discord(f"{symbol} FETCH_TICKER_FAILED: {e}")
            return

        if action == "FLAT":
            closed = await close_position(symbol, price)
            if not closed:
                await notify_discord(f"{symbol} FLAT received but no active position.")
            return

        try:
            positions = await exchange.fetch_positions()
            current_pos = next((p for p in positions or [] if p.get("symbol") == symbol), None)
            
            if current_pos:
                size = float(current_pos.get("info", {}).get("position", {}).get("szi") or 0)
                if size != 0:
                    current_side = "buy" if size > 0 else "sell"
                    target_side = "buy" if action == "BUY" else "sell"
                    
                    if current_side != target_side:
                        logger.info(f"Flipping {symbol} from {current_side} to {target_side}")
                        await close_position(symbol, price)
                        await asyncio.sleep(0.5) 
        except Exception as e:
             logger.error(f"Error checking positions: {e}")
             await notify_discord(f"{symbol} Position Check Error: {e}")
             return

        usdc = await get_perp_usdc()
        if usdc["free"] <= 0:
            msg = f"{symbol} {action} {price:.2f} — Insufficient USDC"
            logger.warning(msg)
            await notify_discord(msg)
            return

        side   = "buy" if action == "BUY" else "sell"
        amount = (usdc["free"] * 0.99 * LEVERAGE) / price if price > 0 else 0

        try:
            order = await exchange.create_order(
                symbol, "market", side, amount, price, {"leverage": LEVERAGE}
            )
            logger.info(f"Order placed: {symbol} {action} {amount:.6f}@{price:.2f}")
            await notify_discord(f"{symbol} {action} {price:.2f}")
        except Exception as e:
            logger.error(f"Order failed: {e}")
            await notify_discord(f"{symbol} {action} {price:.2f} — FAILED: {e}")

async def process_buffered_signals(symbol: str):
    logger.info(f"Buffering signals for {symbol} ({SIGNAL_BUFFER_SECONDS}s)...")
    await asyncio.sleep(SIGNAL_BUFFER_SECONDS)
    
    signals = pending_actions.pop(symbol, set())
    active_timers.discard(symbol)
    
    if not signals:
        return

    logger.info(f"Buffer finished. Signals: {signals}")

    final_action = None
    if "BUY" in signals:
        final_action = "BUY"
    elif "SELL" in signals:
        final_action = "SELL"
    elif "FLAT" in signals:
        final_action = "FLAT"
        
    if final_action:
        await execute_trade_logic(symbol, final_action)
    else:
        logger.warning(f"No valid actions in: {signals}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global exchange
    exchange = ccxt.hyperliquid({
        "walletAddress": WALLET_ADDRESS,
        "privateKey":    HYPE_API_SECRET,
        "enableRateLimit": True,
    })
    
    missing = [k for k in (
        ("TRADINGVIEW_SECRET", TRADINGVIEW_SECRET),
        ("HYPE_API_SECRET",    HYPE_API_SECRET),
        ("WALLET_ADDRESS",     WALLET_ADDRESS),
        ("DISCORD_WEBHOOK_URL",DISCORD_WEBHOOK_URL),
    ) if not k[1]]
    
    if missing:
        msg = f"🚨 Missing env vars: {', '.join(k[0] for k in missing)}"
        logger.critical(msg)
        await notify_discord(msg)
    else:
        logger.info("Env vars loaded.")

    try:
        logger.info("Testing wallet connection...")
        await exchange.fetch_balance()
        logger.info("✅ Wallet connection confirmed!")
        await notify_discord("✅ Wallet connection confirmed! Service is ready.")
    except Exception as e:
        logger.critical(f"❌ Wallet connection FAILED: {e}")
        await notify_discord(f"❌ Wallet connection FAILED: {e}")
    
    yield
    
    try:
        await notify_discord("🛑 Service is shutting down")
        if exchange:
            await exchange.close()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def handle_webhook(payload: WebhookPayload):
    if payload.secret != TRADINGVIEW_SECRET:
        raise HTTPException(401, "Invalid secret")

    raw_action = payload.action.strip().upper()
    symbol = payload.symbol.strip()
    
    if raw_action not in ("BUY", "SELL", "FLAT"):
         raise HTTPException(400, f"Unknown action: {raw_action}")

    logger.info(f"Received: {symbol} -> {raw_action}")

    pending_actions[symbol].add(raw_action)

    if symbol not in active_timers:
        active_timers.add(symbol)
        asyncio.create_task(process_buffered_signals(symbol))
        return {"status": "buffered", "message": f"Queued {raw_action}. Wait {SIGNAL_BUFFER_SECONDS}s"}
    else:
        return {"status": "buffered", "message": f"Added {raw_action} to queue."}