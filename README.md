# TradingBot Webhook Service

A FastAPI-based trading bot webhook service that integrates TradingView alerts with Hyperliquid via CCXT and sends notifications to Discord. Deployed on Railway for easy CI/CD and hosting.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Features](#features)
3. [Prerequisites](#prerequisites)
4. [Installation & Local Development](#installation--local-development)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Deployment on Railway](#deployment-on-railway)
8. [Logging & Monitoring](#logging--monitoring)
9. [Contributing](#contributing)
10. [License](#license)

---

## Project Overview

This project implements a webhook service that listens for TradingView alerts and executes market orders on Hyperliquid’s perpetual markets using CCXT. It also notifies your Discord channel about key events (startup, shutdown, errors, and order executions).

Key components:

* **FastAPI**: Web framework to expose an HTTP `/webhook` endpoint.
* **CCXT Async**: Library to interact with Hyperliquid exchange.
* **HTTPX**: Async HTTP client to send Discord webhook notifications.
* **Railway**: Cloud platform used for deployment, environment variable management, and automatic builds.

---

## Features

* **Secure Webhook Endpoint**: Validates TradingView secret before processing.
* **Market Order Execution**: Supports `BUY`, `SELL`, and `FLAT` (close positions) actions.
* **Leverage Control**: Uses environment-configured leverage for order sizing.
* **Perpetual USDC Balance Check**: Ensures sufficient funds before placing orders.
* **Discord Notifications**: Alerts for startup, shutdown, errors, and each executed trade.
* **Graceful Shutdown**: Closes CCXT exchange connection on application exit.
* **Global Exception Handling**: Catches unhandled exceptions and notifies Discord.

---

## Prerequisites

* Python 3.10+
* A Hyperliquid account with API credentials
* A Discord channel with a webhook URL
* Git (for local development)
* Railway account (for deployment)

---

## Installation & Local Development

1. **Clone the repository**:

   ```bash
   git clone https://github.com/yourusername/tradingbot-webhook.git
   cd tradingbot-webhook
   ```

2. **Create a virtual environment**:

   ```bash
   python -m venv venv
   source venv/bin/activate   # macOS/Linux
   venv\Scripts\activate    # Windows
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**:
   Create a `.env` file in the project root with the following keys:

   ```ini
   TRADINGVIEW_SECRET=your_tradingview_secret
   HYPE_API_SECRET=your_hyperliquid_private_key
   WALLET_ADDRESS=your_hyperliquid_wallet_address
   DISCORD_WEBHOOK_URL=your_discord_webhook_url
   SYMBOL=BTC/USDC:USDC               # optional, defaults to BTC/USDC:USDC
   LEVERAGE=5                         # optional, defaults to 5
   ```

5. **Run the service locally**:

   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   The webhook endpoint will be available at `http://localhost:8000/webhook`.

---

## Configuration

| Environment Variable  | Description                                            | Required | Default         |
| --------------------- | ------------------------------------------------------ | -------- | --------------- |
| `TRADINGVIEW_SECRET`  | Secret key for validating incoming TradingView alerts. | Yes      | —               |
| `HYPE_API_SECRET`     | Hyperliquid API private key.                           | Yes      | —               |
| `WALLET_ADDRESS`      | Your Hyperliquid wallet address.                       | Yes      | —               |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL for notifications.                 | Yes      | —               |
| `SYMBOL`              | Trading pair symbol to trade.                          | No       | `BTC/USDC:USDC` |
| `LEVERAGE`            | Leverage multiplier for position sizing.               | No       | `5`             |

---

## Usage

1. **Configure TradingView**:

   * In TradingView’s alert dialog, set the webhook URL to your deployed endpoint (e.g., `https://your-service.up.railway.app/webhook`).
   * In the `Message` field, send JSON with `secret`, `action`, and optionally `symbol`:

     ```json
     {
       "secret": "${env.TRADINGVIEW_SECRET}",
       "action": "BUY",
       "symbol": "BTC/USDC:USDC"
     }
     ```

2. **Supported Actions**:

   * `BUY`: Opens a new long position.
   * `SELL`: Opens a new short position.
   * `FLAT`: Closes any existing position.

3. **Order Sizing**:

   * Calculates the order amount using available USDC balance and configured leverage.

4. **Notifications**:

   * Startup/shutdown events and unhandled exceptions are posted to Discord.
   * Each executed order sends a Discord message with the trade details.

---

## Deployment on Railway

1. **Create a new Railway project**:

   * Sign in to [Railway](https://railway.app) and create a new project.

2. **Connect your GitHub repository**:

   * Link the `tradingbot-webhook` repository for automatic deploys on push.

3. **Configure Environment Variables**:

   * In the Railway project settings, add all variables from [Configuration](#configuration).

4. **Build & Start Commands**:

   * **Build**: `pip install -r requirements.txt`
   * **Start**: `uvicorn main:app --host 0.0.0.0 --port $PORT`

5. **Deploy**:

   * Trigger a deploy by pushing to your main branch or manually deploying via the Railway UI.
   * After deployment, Railway will provide your service URL (e.g., `https://xyz123.up.railway.app`).

---

## Logging & Monitoring

* **Railway Logs**: View live logs in the Railway dashboard under your service.
* **Discord Alerts**: Receive immediate notifications for critical events and errors.

### Hyperliquid rate limits (HTTP 429)

The bot keeps CCXT's rate limiter enabled and retries rate-limited position,
balance, and ticker reads up to three times, waiting 10, 20, then 40 seconds
(plus up to one second of jitter per retry). Startup and daily checks use the
same retry handling. Reads within this process share the backoff pause.

If a read still fails, the current action stops and reports the error. An order
submission is never automatically retried, and a failed close stops a side
switch. Position data is reused for the close within the same decision, and
prices are fetched after read delays before submitting orders.

Hyperliquid's REST limits are shared per IP address. Multiple workers, replicas,
or services sharing an outbound IP can still exhaust that limit; CCXT's limiter
and this retry lock only coordinate one process. If 429s persist, check Railway
replica/worker counts and other traffic using the same outbound IP. See the
[Hyperliquid rate-limit documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).

Run the offline regression suite with `python -m unittest discover -s tests -v`.

---

## Contributing

1. Fork the repository.
2. Create a new branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
