"""
guardrails.py
==============
Fat-finger and runaway-automation protection. Two independent checks:

1. max_trade_usd     -- hard cap on a single trade's size
2. max_daily_volume_usd -- rolling 24h cap across all trades (manual + auto)

Both are None (unlimited) by default -- a bot serving real users should set
sane platform-wide defaults in addition to letting users tighten further.
Call `check_guardrails()` BEFORE calling execute_market_order for every
trade path (manual buy/sell, limit/TP/SL fills, copy-trade auto-copy).
After a trade succeeds, call `log_trade()` so subsequent volume checks see it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from models.db import TradeLog, User, async_session


class GuardrailViolation(Exception):
    """Raised when a trade would breach a configured limit."""


async def check_guardrails(user_id: int, usd_value: float) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return  # let the caller's own not-found handling take over

        if user.max_trade_usd and usd_value > user.max_trade_usd:
            raise GuardrailViolation(
                f"This trade (${usd_value:,.2f}) exceeds your per-trade limit "
                f"of ${user.max_trade_usd:,.2f}. Adjust it in wallet settings if intended."
            )

        if user.max_daily_volume_usd:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            result = await session.execute(
                select(func.coalesce(func.sum(TradeLog.usd_value), 0.0)).where(
                    TradeLog.user_id == user_id, TradeLog.created_at >= since
                )
            )
            volume_so_far = result.scalar_one()
            if volume_so_far + usd_value > user.max_daily_volume_usd:
                raise GuardrailViolation(
                    f"This trade would push your rolling 24h volume to "
                    f"${volume_so_far + usd_value:,.2f}, above your "
                    f"${user.max_daily_volume_usd:,.2f} limit."
                )


async def needs_extra_confirmation(user_id: int, usd_value: float) -> bool:
    """
    Soft check (not a hard block): should the UI ask "are you sure?" before
    firing this trade. Used for manual buys/sells above the user's threshold;
    NOT applied to copy-trade auto-copy or TP/SL fills, since those are
    pre-authorized by the user when they configured the automation.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.require_confirm_above_usd:
            return False
        return usd_value > user.require_confirm_above_usd


async def log_trade(
    user_id: int, chain: str, token_address: str, side: str, usd_value: float,
    tx_signature: str, source: str = "manual",
) -> None:
    async with async_session() as session:
        session.add(
            TradeLog(
                user_id=user_id, chain=chain, token_address=token_address, side=side,
                usd_value=usd_value, tx_signature=tx_signature, source=source,
            )
        )
        await session.commit()
