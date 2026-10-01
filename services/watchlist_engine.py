"""
watchlist_engine.py
====================
Scheduler tick: for every WatchlistItem with alert_on_pct_move set, fetch
current price and notify the user if it has moved >= that % since the last
alert (not since item creation -- so alerts keep firing on continued moves,
e.g. every additional 5% leg up or down).
"""
from __future__ import annotations

from sqlalchemy import select

from models.db import WatchlistItem, async_session
from services.holdings import get_price_usd


async def check_watchlist(notifier) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(WatchlistItem).where(WatchlistItem.alert_on_pct_move.is_not(None))
        )
        items = list(result.scalars().all())

    for item in items:
        try:
            current_price = await get_price_usd(item.chain, item.token_address)
            if current_price is None:
                continue

            baseline = item.last_alert_price_usd or item.reference_price_usd
            if baseline is None:
                async with async_session() as session:
                    db_item = await session.get(WatchlistItem, item.id)
                    db_item.reference_price_usd = current_price
                    db_item.last_alert_price_usd = current_price
                    await session.commit()
                continue

            pct_move = ((current_price - baseline) / baseline) * 100
            if abs(pct_move) >= item.alert_on_pct_move:
                direction = "🟢 pumped" if pct_move > 0 else "🔴 dumped"
                await notifier(
                    item.user_id,
                    f"🔔 {item.token_symbol or item.token_address[:6]} {direction} "
                    f"{pct_move:+.1f}% (now ${current_price:.6f})",
                )
                async with async_session() as session:
                    db_item = await session.get(WatchlistItem, item.id)
                    db_item.last_alert_price_usd = current_price
                    await session.commit()
        except Exception:
            continue
