import importlib
import asyncio
import sys
import types
import unittest


def install_import_stubs():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return lambda func: func

    class HTTPException(Exception):
        pass

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    pydantic.BaseModel = BaseModel
    pydantic.Field = lambda default=None, **kwargs: default
    sys.modules["pydantic"] = pydantic

    ccxt = types.ModuleType("ccxt")
    ccxt.hyperliquid = object
    async_support = types.ModuleType("ccxt.async_support")
    async_support.hyperliquid = object
    ccxt.async_support = async_support
    sys.modules["ccxt"] = ccxt
    sys.modules["ccxt.async_support"] = async_support

    httpx = types.ModuleType("httpx")
    sys.modules["httpx"] = httpx


def import_webhook_server():
    sys.modules.pop("webhook_server", None)
    install_import_stubs()
    return importlib.import_module("webhook_server")


class ExchangeConfigTest(unittest.TestCase):
    def test_swap_only_config_avoids_spot_market_parse_failure(self):
        webhook_server = import_webhook_server()

        class HyperliquidMarketLoader:
            def __init__(self, config):
                options = config.get("options", {})
                fetch_markets = options.get(
                    "fetchMarkets",
                    {"types": ["spot", "swap", "hip3"]},
                )
                self.market_types = fetch_markets["types"]

            def load_markets(self):
                if "spot" in self.market_types:
                    mapped_base = None
                    mapped_quote = "USDC"
                    return mapped_base + "/" + mapped_quote
                return ["BTC/USDC:USDC"]

        with self.assertRaisesRegex(TypeError, "NoneType.*str|str.*NoneType"):
            HyperliquidMarketLoader({}).load_markets()

        config = webhook_server.build_hyperliquid_config("0xabc", "0xsecret")

        self.assertEqual(
            HyperliquidMarketLoader(config).load_markets(),
            ["BTC/USDC:USDC"],
        )

    def test_hyperliquid_config_only_loads_swap_markets(self):
        webhook_server = import_webhook_server()

        config = webhook_server.build_hyperliquid_config("0xabc", "0xsecret")

        self.assertEqual(config["walletAddress"], "0xabc")
        self.assertEqual(config["privateKey"], "0xsecret")
        self.assertTrue(config["enableRateLimit"])
        self.assertEqual(config["options"]["defaultType"], "swap")
        self.assertEqual(config["options"]["fetchMarkets"]["types"], ["swap"])

    def test_trade_logic_fetches_ticker_as_swap_market(self):
        webhook_server = import_webhook_server()

        class FakeExchange:
            def __init__(self):
                self.ticker_calls = []

            async def fetch_ticker(self, symbol, params=None):
                self.ticker_calls.append((symbol, params))
                return {"last": 100.0}

            async def fetch_positions(self):
                return []

        notifications = []

        async def fake_notify_discord(content):
            notifications.append(content)

        fake_exchange = FakeExchange()
        webhook_server.exchange = fake_exchange
        webhook_server.notify_discord = fake_notify_discord

        asyncio.run(webhook_server.execute_trade_logic("BTC/USDC:USDC", "FLAT"))

        self.assertEqual(
            fake_exchange.ticker_calls,
            [("BTC/USDC:USDC", {"type": "swap"})],
        )
        self.assertEqual(
            notifications,
            ["BTC/USDC:USDC FLAT received but no active position."],
        )


if __name__ == "__main__":
    unittest.main()
