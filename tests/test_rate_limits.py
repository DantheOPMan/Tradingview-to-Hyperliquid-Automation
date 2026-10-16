import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_exchange_config import import_webhook_server


SYMBOL = "BTC/USDC:USDC"


class RateLimitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = import_webhook_server()
        self.server.LEVERAGE = 5
        self.server.notify_discord = AsyncMock()
        self.exchange = self.server.exchange = AsyncMock()
        self.exchange.fetch_ticker.return_value = {"last": 100.0}
        self.exchange.fetch_positions.return_value = []
        self.exchange.fetch_balance.return_value = {
            "USDC": {"total": 100.0, "free": 100.0, "used": 0.0}
        }
        self.error = self.server.ccxt.RateLimitExceeded(
            "hyperliquid POST https://api.hyperliquid.xyz/info 429 Too Many Requests null"
        )
        self.real_sleep = asyncio.sleep
        self.sleep_patch = patch.object(self.server.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def test_position_429_recovers_before_placing_one_order(self):
        self.exchange.fetch_positions.side_effect = [self.error, []]

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.assertEqual(self.exchange.fetch_positions.await_count, 2)
        self.sleep.assert_awaited_once()
        self.exchange.create_order.assert_awaited_once_with(
            SYMBOL, "market", "buy", 4.95, 100.0, {"leverage": 5}
        )
        self.server.notify_discord.assert_awaited_once_with(f"{SYMBOL} BUY 100.00")

    async def test_persistent_429_stops_after_bounded_exponential_backoff(self):
        self.exchange.fetch_positions.side_effect = self.error

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.assertEqual(self.exchange.fetch_positions.await_count, 4)
        waits = [call.args[0] for call in self.sleep.await_args_list]
        self.assertEqual(len(waits), 3)
        for delay, minimum in zip(waits, [10, 20, 40]):
            self.assertGreaterEqual(delay, minimum)
            self.assertLessEqual(delay, minimum + 1)
        self.exchange.create_order.assert_not_awaited()
        self.exchange.fetch_balance.assert_not_awaited()
        self.assertIn("Position Check Error", self.server.notify_discord.await_args.args[0])

    async def test_other_position_errors_are_not_retried(self):
        self.exchange.fetch_positions.side_effect = ValueError("invalid wallet")

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.exchange.fetch_positions.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.exchange.create_order.assert_not_awaited()

    async def test_ticker_and_balance_reads_recover_from_429(self):
        self.exchange.fetch_ticker.side_effect = [self.error, {"last": 100.0}]
        self.exchange.fetch_balance.side_effect = [
            self.error, {"USDC": {"total": 100, "free": 100, "used": 0}}
        ]

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.assertEqual(self.exchange.fetch_ticker.await_count, 2)
        self.assertEqual(self.exchange.fetch_balance.await_count, 2)
        self.exchange.create_order.assert_awaited_once()

    async def test_balance_failure_is_not_reported_as_insufficient_funds(self):
        self.exchange.fetch_balance.side_effect = self.error

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.assertEqual(self.exchange.fetch_balance.await_count, 4)
        self.exchange.create_order.assert_not_awaited()
        message = self.server.notify_discord.await_args.args[0]
        self.assertIn("BALANCE_CHECK_FAILED", message)
        self.assertNotIn("Insufficient USDC", message)

    async def test_opening_price_is_fetched_after_balance_recovers(self):
        self.exchange.fetch_balance.side_effect = [self.error, {"USDC": {"free": 100}}]

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        calls = [call[0] for call in self.exchange.mock_calls]
        self.assertLess(max(i for i, name in enumerate(calls) if name == "fetch_balance"),
                        calls.index("fetch_ticker"))
        self.exchange.create_order.assert_awaited_once()

    async def test_flip_reuses_position_snapshot_and_refreshes_opening_price(self):
        self.exchange.fetch_positions.return_value = [
            {"symbol": SYMBOL, "info": {"position": {"szi": "-2"}}}
        ]
        self.exchange.fetch_ticker.side_effect = [{"last": 100.0}, {"last": 110.0}]

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.exchange.fetch_positions.assert_awaited_once()
        self.assertEqual(self.exchange.create_order.await_count, 2)
        close, opening = self.exchange.create_order.await_args_list
        self.assertEqual(close.args, (SYMBOL, "market", "buy", 2.0, 100.0,
                                     {"leverage": 5, "reduceOnly": True}))
        self.assertEqual(opening.args[4], 110.0)
        self.assertAlmostEqual(opening.args[3], 495 / 110)

    async def test_failed_flip_close_does_not_submit_an_opening_order(self):
        self.exchange.fetch_positions.return_value = [
            {"symbol": SYMBOL, "info": {"position": {"szi": "-2"}}}
        ]
        self.exchange.create_order.side_effect = self.error

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.exchange.create_order.assert_awaited_once()
        self.exchange.fetch_balance.assert_not_awaited()
        self.sleep.assert_not_awaited()

    async def test_failed_flat_close_is_not_reported_as_no_active_position(self):
        self.exchange.fetch_positions.side_effect = self.error

        await self.server.execute_trade_logic(SYMBOL, "FLAT")

        messages = [call.args[0] for call in self.server.notify_discord.await_args_list]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("no active position", messages[0])
        self.exchange.create_order.assert_not_awaited()

    async def test_opening_order_is_never_retried(self):
        self.exchange.create_order.side_effect = self.error

        await self.server.execute_trade_logic(SYMBOL, "BUY")

        self.exchange.create_order.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_other_reads_wait_while_a_rate_limited_read_is_backing_off(self):
        resume = asyncio.Event()
        async def wait_for_resume(delay):
            await resume.wait()

        self.sleep.side_effect = wait_for_resume
        self.exchange.fetch_positions.side_effect = [self.error, []]
        trade = asyncio.create_task(self.server.execute_trade_logic(SYMBOL, "BUY"))
        tasks = [trade]
        try:
            await self.real_sleep(0)
            self.assertFalse(trade.done(), "The signal should wait for a retry")
            balance = asyncio.create_task(self.server.get_perp_usdc())
            tasks.append(balance)
            await self.real_sleep(0)
            self.exchange.fetch_balance.assert_not_awaited()
            resume.set()
            await asyncio.gather(*tasks)
            self.exchange.create_order.assert_awaited_once()
        finally:
            resume.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
