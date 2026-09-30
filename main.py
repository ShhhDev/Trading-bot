from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from handlers import copy_trade, guardrails_menu, onboarding, swap, trading, unlock, wallet_menu, watchlist
from models.db import init_db
from services.bot_actions import copy_trade_order_executor, make_notifier
from services.copy_trade_engine import CopyTradeEngine
from services.copy_trade_poller import poll_copy_trade_sources
from services.deposit_watcher import check_deposits
from services.order_engine import check_pending_orders
from services.session_keys import session_keys
from services.watchlist_engine import check_watchlist

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()

    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(onboarding.router)
    dp.include_router(wallet_menu.router)
    dp.include_router(trading.router)
    dp.include_router(swap.router)
    dp.include_router(watchlist.router)
    dp.include_router(copy_trade.router)
    dp.include_router(unlock.router)
    dp.include_router(guardrails_menu.router)

    # All background engines (order_engine, copy_trade_engine, watchlist_engine,
    # deposit_watcher) work in terms of the internal DB User.id. `notifier`
    # resolves that to a real Telegram chat id before sending -- see
    # services/bot_actions.py for the resolution + caching logic.
    notifier = make_notifier(bot)
    copy_engine = CopyTradeEngine(notifier=notifier, order_executor=copy_trade_order_executor)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_pending_orders, "interval", seconds=10, args=[notifier],
        id="pending_orders", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        check_watchlist, "interval", seconds=20, args=[notifier],
        id="watchlist_alerts", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        check_deposits, "interval", seconds=15, args=[notifier],
        id="deposit_watch", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        poll_copy_trade_sources, "interval", seconds=12, args=[copy_engine],
        id="copy_trade_poll", max_instances=1, coalesce=True,
    )
    scheduler.add_job(session_keys.sweep_expired, "interval", seconds=60, id="session_key_sweep")
    scheduler.start()

    logger.info("Bot starting (polling mode)...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
