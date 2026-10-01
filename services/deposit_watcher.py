"""
deposit_watcher.py
===================
Polling-based deposit detector. For each active wallet, tracks the last-seen
transaction signature/hash and diffs on each tick to find new incoming
transfers, notifying the owning user.

This is the "works everywhere, no public HTTP endpoint required" approach --
appropriate for a bot running on a VPS/behind NAT. If you later stand up a
webhook receiver (see copy_trade_engine.py's docstring for the same tradeoff
on the copy-trade side), you can retire this poller and get faster, cheaper
detection instead.

Scale note: this does one RPC call per active wallet per tick. Fine for
tens-to-low-hundreds of users on a decent RPC provider; beyond that, prefer
Helius/tonapi webhooks.
"""
from __future__ import annotations

from sqlalchemy import select

from models.db import Wallet, async_session
from services.solana_client import solana_client
from services.ton_client import ton_client

# In-memory "last seen" cursor per wallet. Lost on restart -- acceptable
# tradeoff (worst case: a missed notification for one restart window, not
# a missed deposit, since balances are always fetched fresh on-demand).
_last_seen_sol: dict[int, str] = {}   # wallet.id -> last tx signature
_last_seen_ton: dict[int, str] = {}   # wallet.id -> last tx hash/lt


async def check_deposits(notifier) -> None:
    async with async_session() as session:
        result = await session.execute(select(Wallet).where(Wallet.is_active == True))  # noqa: E712
        wallets = list(result.scalars().all())

    for wallet in wallets:
        try:
            if wallet.chain.value == "SOL":
                await _check_sol_wallet(wallet, notifier)
            elif wallet.chain.value == "TON":
                await _check_ton_wallet(wallet, notifier)
        except Exception:
            # A single wallet's RPC hiccup shouldn't stop the whole sweep.
            continue


async def _check_sol_wallet(wallet: Wallet, notifier) -> None:
    sigs = await solana_client.get_recent_signatures(wallet.public_address, limit=10)
    if not sigs:
        return

    newest_sig = sigs[0]["signature"]
    last_seen = _last_seen_sol.get(wallet.id)

    if last_seen is None:
        # First tick for this wallet: just set the cursor, don't spam
        # historical deposits from before the bot was watching.
        _last_seen_sol[wallet.id] = newest_sig
        return

    if newest_sig == last_seen:
        return  # nothing new

    # Collect signatures newer than last_seen (they come back newest-first)
    new_sigs = []
    for entry in sigs:
        if entry["signature"] == last_seen:
            break
        new_sigs.append(entry["signature"])

    _last_seen_sol[wallet.id] = newest_sig

    if new_sigs:
        # NOTE: distinguishing "incoming deposit" from "our own outgoing
        # trade tx" requires parsing each tx's balance changes relative to
        # this wallet. Left as the next-level enrichment; for now this
        # notifies on ANY new activity, labelled generically.
        await notifier(
            wallet.user_id,
            f"📥 New activity detected on your SOL wallet "
            f"({len(new_sigs)} new transaction(s)). Check your balance in 👛 My Wallet.",
        )


async def _check_ton_wallet(wallet: Wallet, notifier) -> None:
    txs = await ton_client.get_recent_transactions(wallet.public_address, limit=10)
    if not txs:
        return

    newest_id = txs[0].get("hash") or txs[0].get("lt")
    last_seen = _last_seen_ton.get(wallet.id)

    if last_seen is None:
        _last_seen_ton[wallet.id] = newest_id
        return

    if newest_id == last_seen:
        return

    new_txs = []
    for tx in txs:
        tx_id = tx.get("hash") or tx.get("lt")
        if tx_id == last_seen:
            break
        new_txs.append(tx)

    _last_seen_ton[wallet.id] = newest_id

    if new_txs:
        await notifier(
            wallet.user_id,
            f"📥 New activity detected on your TON wallet "
            f"({len(new_txs)} new transaction(s)). Check your balance in 👛 My Wallet.",
        )
