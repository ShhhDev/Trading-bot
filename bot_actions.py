"""
bot_actions.py
===============
Bridges the DB-internal `User.id` used throughout services/ (order_engine,
copy_trade_engine, watchlist_engine) to actual Telegram delivery, and wires
copy-trade AUTO_COPY decisions into real trade execution.

Kept separate from main.py so it's independently testable and so main.py
stays a thin wiring/startup file.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select

from models.db import Chain, User, Wallet, async_session
from services.copy_trade_engine import EventSide
from services.order_engine import InsufficientUnlockError, execute_market_order
from services.solana_client import solana_client
from services.ton_client import ton_client

logger = logging.getLogger(__name__)

# Short-lived cache so every notification doesn't hit the DB for the
# user_id -> telegram_id lookup. Small dataset, fine to keep in memory.
_telegram_id_cache: dict[int, int] = {}


async def _resolve_telegram_id(user_id: int) -> int | None:
    if user_id in _telegram_id_cache:
        return _telegram_id_cache[user_id]
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return None
        _telegram_id_cache[user_id] = user.telegram_id
        return user.telegram_id


def make_notifier(bot: Bot):
    """
    Returns an async fn(user_id: int, text: str, **kwargs) that resolves the
    internal DB user_id to a Telegram chat id and sends the message.
    All engines (order_engine, copy_trade_engine, watchlist_engine,
    deposit_watcher) pass internal user_id -- this is the single place that
    translates to an actual Telegram send.
    """
    async def notifier(user_id: int, text: str, **kwargs) -> None:
        telegram_id = await _resolve_telegram_id(user_id)
        if telegram_id is None:
            logger.warning("Notifier: no telegram_id found for internal user_id=%s", user_id)
            return
        try:
            await bot.send_message(telegram_id, text, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to notify user_id=%s (tg=%s): %s", user_id, telegram_id, e)

    return notifier


async def _usd_to_native_amount(chain: str, usd_amount: float) -> float:
    """Converts a target USD spend into native units (SOL or TON) for a BUY."""
    if chain == "SOL":
        SOL_MINT = "So11111111111111111111111111111111111111112"
        price = await solana_client.get_token_price_usd(SOL_MINT)
    else:
        # TON doesn't have a jetton price lookup for the native coin itself;
        # use a stable-pegged jetton or a dedicated TON/USD price source.
        # Placeholder: treat as unavailable until wired to a TON price feed.
        price = None

    if not price:
        raise RuntimeError(f"Could not resolve {chain} native price for USD conversion")
    return usd_amount / price


async def copy_trade_order_executor(
    user_id: int, chain: str, token_address: str, side: EventSide, usd_amount: float, slippage_bps: int
) -> str:
    """
    Wired into CopyTradeEngine(order_executor=...). Called only for
    AUTO_COPY configs whose trigger conditions already passed.

    IMPORTANT: AUTO_COPY requires the user's wallet to already be unlocked
    (via session_keys, see handlers/unlock.py) since there's no one present
    to type a PIN when a trader's trade fires at 3am. If the wallet is
    locked, this raises InsufficientUnlockError, which the caller
    (CopyTradeEngine.handle_event) catches and turns into a failure
    notification telling the user to unlock. This is a deliberate tradeoff:
    we will NOT silently fall back to a lower-security signing path just to
    make auto-copy always work.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.chain == Chain(chain))
        )
        wallet = result.scalar_one_or_none()

    if not wallet:
        raise RuntimeError(f"User {user_id} has no {chain} wallet to copy-trade with")

    if side == EventSide.BUY:
        native_amount = await _usd_to_native_amount(chain, usd_amount)
        return await execute_market_order(
            user_id=user_id, wallet=wallet, token_address=token_address, side="BUY",
            amount_in=native_amount, slippage_bps=slippage_bps, source="copy_trade",
        )
    else:
        # SELL: copy-selling should be sized against the USER's own holding
        # of this token, not the trader's, since they may hold a different
        # amount (or none at all -- in which case there's nothing to copy).
        from services.holdings import get_holding_amount, get_price_usd

        held = await get_holding_amount(chain, wallet.public_address, token_address)
        if held <= 0:
            raise RuntimeError("You don't hold this token, so there's nothing to copy-sell")

        price = await get_price_usd(chain, token_address)
        if not price:
            # Can't size by USD without a price; fall back to selling the
            # full position, which is the safer default for a SELL copy.
            sell_amount = held
        else:
            target_amount = usd_amount / price
            sell_amount = min(target_amount, held)

        return await execute_market_order(
            user_id=user_id, wallet=wallet, token_address=token_address, side="SELL",
            amount_in=sell_amount, slippage_bps=slippage_bps, source="copy_trade",
        )
