"""
copy_trade_engine.py
=====================
Polls (or receives webhooks for) followed trader wallets, detects buy/sell
events, and for each user following that trader:
  - NOTIFY_ONLY  -> sends a Telegram alert
  - AUTO_COPY    -> sizes and submits a matching trade via the order engine,
                     using the user's OWN wallet, gated by their own
                     min_trader_buy_usd / copy_amount_usd / slippage settings

Detection strategy (production):
  - Solana: Helius webhooks (enhanced transaction parsing) are far more
    reliable than polling getSignaturesForAddress. This module is written
    against a generic `ChainEvent` so you can swap the source freely.
  - TON: tonapi.io supports webhook subscriptions on an account; same idea.

This file focuses on the DECISION logic; wiring the actual event source is
marked clearly below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import select

from models.db import CopyMode, CopyTradeConfig, async_session
from services.order_engine import InsufficientUnlockError


class EventSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class ChainEvent:
    chain: str  # "SOL" | "TON"
    trader_address: str
    side: EventSide
    token_address: str
    token_symbol: str | None
    usd_value: float
    token_amount: float
    tx_signature: str
    occurred_at: datetime


class CopyTradeEngine:
    def __init__(self, notifier, order_executor) -> None:
        """
        notifier: async fn(telegram_user_id: int, text: str, **kwargs) -> None
        order_executor: async fn(user_id: int, chain: str, token_address: str,
                                  side: EventSide, usd_amount: float,
                                  slippage_bps: int) -> str (tx sig or error)
        """
        self.notifier = notifier
        self.order_executor = order_executor

    async def handle_event(self, event: ChainEvent) -> None:
        configs = await self._get_active_followers(event.chain, event.trader_address)
        for cfg in configs:
            if event.usd_value < cfg.min_trader_buy_usd:
                continue
            if event.side == EventSide.SELL and not cfg.also_copy_sells:
                continue

            trader_tag = cfg.label or event.trader_address[:6]

            if cfg.mode == CopyMode.NOTIFY_ONLY:
                await self._notify(cfg.user_id, event, trader_tag)
                continue

            # AUTO_COPY
            copy_usd = self._resolve_copy_amount(cfg, event)
            try:
                tx_sig = await self.order_executor(
                    user_id=cfg.user_id,
                    chain=event.chain,
                    token_address=event.token_address,
                    side=event.side,
                    usd_amount=copy_usd,
                    slippage_bps=cfg.max_slippage_bps,
                )
                await self.notifier(
                    cfg.user_id,
                    f"🔁 Copied {trader_tag}: {event.side.value} "
                    f"{event.token_symbol or event.token_address[:6]} for ~${copy_usd:.2f}\n"
                    f"Tx: {tx_sig}",
                )
            except InsufficientUnlockError:
                await self.notifier(
                    cfg.user_id,
                    f"⚠️ {trader_tag} just {event.side.value.lower()} "
                    f"{event.token_symbol or event.token_address[:6]}, but your wallet is "
                    f"locked so I couldn't auto-copy it. Unlock with your PIN (👛 My Wallet) "
                    f"if you want future trades from this wallet to auto-copy.",
                )
            except Exception as e:  # noqa: BLE001
                await self.notifier(
                    cfg.user_id,
                    f"⚠️ Failed to copy {trader_tag}'s {event.side.value} on "
                    f"{event.token_symbol or event.token_address[:6]}: {e}",
                )

    def _resolve_copy_amount(self, cfg: CopyTradeConfig, event: ChainEvent) -> float:
        if cfg.copy_amount_is_pct_of_trader:
            # e.g. user says "copy at 10% of whatever size the trader used"
            pct = cfg.copy_amount_usd  # reused field as a percentage in this mode
            return event.usd_value * (pct / 100.0)
        return cfg.copy_amount_usd

    async def _notify(self, user_id: int, event: ChainEvent, trader_tag: str) -> None:
        verb = "bought" if event.side == EventSide.BUY else "sold"
        await self.notifier(
            user_id,
            f"👀 {trader_tag} just {verb} "
            f"{event.token_symbol or event.token_address[:6]} "
            f"(~${event.usd_value:,.2f})",
        )

    async def _get_active_followers(self, chain: str, trader_address: str) -> list[CopyTradeConfig]:
        async with async_session() as session:
            result = await session.execute(
                select(CopyTradeConfig).where(
                    CopyTradeConfig.chain == chain,
                    CopyTradeConfig.trader_address == trader_address,
                    CopyTradeConfig.is_active == True,  # noqa: E712
                )
            )
            return list(result.scalars().all())


# ----------------------------------------------------------------------------
# EVENT SOURCE WIRING (production TODO):
#
# Solana (Helius webhook, recommended):
#   1. Register a webhook per unique followed trader_address (or one webhook
#      covering all addresses you currently track, re-registered on change)
#      at https://docs.helius.dev/webhooks
#   2. Your FastAPI/aiohttp webhook endpoint parses the payload, builds a
#      ChainEvent, and calls `copy_trade_engine.handle_event(event)`.
#
# TON (tonapi.io webhook or polling fallback):
#   Same shape via tonapi's account subscription API.
#
# Fallback (no webhook infra yet): APScheduler job every N seconds calls
# solana_client.get_recent_signatures(trader_address) / ton_client's
# equivalent, diffs against last-seen signature per trader, parses new txs,
# and emits ChainEvents. Slower and rate-limit-heavier than webhooks.
# ----------------------------------------------------------------------------
