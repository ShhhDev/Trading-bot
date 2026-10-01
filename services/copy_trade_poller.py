"""
copy_trade_poller.py
=====================
Polling fallback event source for copy_trade_engine.py. Watches every
distinct trader_address currently followed by at least one active
CopyTradeConfig, diffs new transactions since last tick, parses them into
ChainEvent objects, and feeds them to CopyTradeEngine.handle_event().

This works without any public HTTP endpoint -- appropriate default for a
self-hosted bot. Swap in the Helius/tonapi webhook receiver
(webhook_server.py) for lower latency once you have inbound HTTP set up;
both can run simultaneously without conflict since handle_event() is
idempotent-ish per tx signature (dedup is handled by the last-seen cursor
here -- if running both poller and webhook, add a processed-signatures
cache to avoid double notifications/copies).

SOL parsing note: getSignaturesForAddress + getTransaction gives you raw
instruction data, not a clean "bought token X for $Y" summary. Getting a
reliable USD value per trade from raw parsing is a real chunk of work
(you need: token balance deltas from pre/postTokenBalances, then a price
lookup for the involved mint at that moment). Helius's enhanced
transactions API (parsed "swap" events) does this for you and is the
practical path for production; this poller uses it when HELIUS_API_KEY is
set, and falls back to a best-effort raw-balance-delta parse otherwise.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
from sqlalchemy import select

from config import settings
from models.db import CopyTradeConfig, async_session
from services.copy_trade_engine import ChainEvent, EventSide, CopyTradeEngine
from services.solana_client import solana_client
from services.token_metadata import get_token_metadata
from services.ton_client import ton_client

_last_seen_sol: dict[str, str] = {}  # trader_address -> last tx signature
_last_seen_ton: dict[str, str] = {}  # trader_address -> last tx hash/lt

SOL_MINT = "So11111111111111111111111111111111111111112"


async def _get_followed_traders() -> dict[str, list[str]]:
    """Returns {chain: [trader_address, ...]} for all actively-followed traders."""
    async with async_session() as session:
        result = await session.execute(
            select(CopyTradeConfig.chain, CopyTradeConfig.trader_address).where(
                CopyTradeConfig.is_active == True  # noqa: E712
            )
        )
        rows = result.all()

    by_chain: dict[str, set[str]] = {"SOL": set(), "TON": set()}
    for chain, address in rows:
        by_chain[chain.value].add(address)
    return {k: list(v) for k, v in by_chain.items()}


async def poll_copy_trade_sources(engine: CopyTradeEngine) -> None:
    traders = await _get_followed_traders()

    for address in traders.get("SOL", []):
        try:
            await _poll_sol_trader(address, engine)
        except Exception:
            continue  # one trader's RPC hiccup shouldn't block the others

    for address in traders.get("TON", []):
        try:
            await _poll_ton_trader(address, engine)
        except Exception:
            continue


async def _poll_sol_trader(trader_address: str, engine: CopyTradeEngine) -> None:
    sigs = await solana_client.get_recent_signatures(trader_address, limit=10)
    if not sigs:
        return

    newest_sig = sigs[0]["signature"]
    last_seen = _last_seen_sol.get(trader_address)

    if last_seen is None:
        _last_seen_sol[trader_address] = newest_sig
        return  # don't backfill history from before we started watching

    if newest_sig == last_seen:
        return

    new_sigs = []
    for entry in sigs:
        if entry["signature"] == last_seen:
            break
        new_sigs.append(entry["signature"])
    _last_seen_sol[trader_address] = newest_sig

    for sig in reversed(new_sigs):  # oldest first, preserves trade order
        event = await _parse_sol_tx(trader_address, sig)
        if event:
            await engine.handle_event(event)


async def _parse_sol_tx(trader_address: str, signature: str) -> ChainEvent | None:
    """
    Best-effort parse of a Solana tx into a buy/sell ChainEvent.
    Uses Helius's enhanced-transactions endpoint when available (much more
    reliable); otherwise falls back to raw getTransaction balance-delta
    parsing, which handles the common "simple swap" case but will miss more
    exotic instruction patterns (multi-hop routes, CPI-wrapped swaps, etc).
    """
    if settings.HELIUS_API_KEY:
        event = await _parse_via_helius(trader_address, signature)
        if event:
            return event

    return await _parse_via_raw_rpc(trader_address, signature)


async def _parse_via_helius(trader_address: str, signature: str) -> ChainEvent | None:
    url = f"https://api.helius.xyz/v0/transactions?api-key={settings.HELIUS_API_KEY}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={"transactions": [signature]}, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()
    except Exception:
        return None

    if not data:
        return None
    tx = data[0]
    if tx.get("type") != "SWAP":
        return None

    events = tx.get("events", {}).get("swap")
    if not events:
        return None

    token_in = events.get("tokenInputs", [{}])[0] if events.get("tokenInputs") else None
    token_out = events.get("tokenOutputs", [{}])[0] if events.get("tokenOutputs") else None
    native_in = events.get("nativeInput")
    native_out = events.get("nativeOutput")

    # BUY = trader spent SOL, received a token. SELL = trader spent a token, received SOL.
    if native_in and token_out:
        side = EventSide.BUY
        token_address = token_out.get("mint")
        token_amount = float(token_out.get("tokenAmount", 0))
        usd_value = await _sol_lamports_to_usd(int(native_in.get("amount", 0)))
    elif token_in and native_out:
        side = EventSide.SELL
        token_address = token_in.get("mint")
        token_amount = float(token_in.get("tokenAmount", 0))
        usd_value = await _sol_lamports_to_usd(int(native_out.get("amount", 0)))
    else:
        return None  # token-to-token swap; skip for now, or extend to handle

    if not token_address:
        return None

    meta = await get_token_metadata("SOL", token_address)
    return ChainEvent(
        chain="SOL",
        trader_address=trader_address,
        side=side,
        token_address=token_address,
        token_symbol=meta.get("symbol"),
        usd_value=usd_value,
        token_amount=token_amount,
        tx_signature=signature,
        occurred_at=datetime.now(timezone.utc),
    )


async def _sol_lamports_to_usd(lamports: int) -> float:
    sol_price = await solana_client.get_token_price_usd(SOL_MINT)
    if sol_price is None:
        return 0.0
    return (lamports / 1_000_000_000) * sol_price


async def _parse_via_raw_rpc(trader_address: str, signature: str) -> ChainEvent | None:
    """
    Fallback when no Helius key is set. Parses pre/post token balances from
    getTransaction to infer a simple buy/sell. Misses complex routes.
    """
    try:
        result = await solana_client._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
    except Exception:
        return None

    if not result or "meta" not in result:
        return None

    meta = result["meta"]
    pre_sol = meta.get("preBalances", [0])[0]
    post_sol = meta.get("postBalances", [0])[0]
    sol_delta = (post_sol - pre_sol) / 1_000_000_000

    pre_tokens = {t["mint"]: t for t in meta.get("preTokenBalances", []) if t.get("owner") == trader_address}
    post_tokens = {t["mint"]: t for t in meta.get("postTokenBalances", []) if t.get("owner") == trader_address}

    for mint in set(pre_tokens) | set(post_tokens):
        pre_amt = float(pre_tokens.get(mint, {}).get("uiTokenAmount", {}).get("uiAmountString") or 0)
        post_amt = float(post_tokens.get(mint, {}).get("uiTokenAmount", {}).get("uiAmountString") or 0)
        token_delta = post_amt - pre_amt

        if token_delta > 0 and sol_delta < 0:
            side = EventSide.BUY
        elif token_delta < 0 and sol_delta > 0:
            side = EventSide.SELL
        else:
            continue

        sol_price = await solana_client.get_token_price_usd(SOL_MINT)
        usd_value = abs(sol_delta) * sol_price if sol_price else 0.0
        meta_info = await get_token_metadata("SOL", mint)

        return ChainEvent(
            chain="SOL", trader_address=trader_address, side=side, token_address=mint,
            token_symbol=meta_info.get("symbol"), usd_value=usd_value,
            token_amount=abs(token_delta), tx_signature=signature,
            occurred_at=datetime.now(timezone.utc),
        )

    return None


async def _poll_ton_trader(trader_address: str, engine: CopyTradeEngine) -> None:
    """
    TON parsing via tonapi's decoded transaction actions, which already
    label jetton swaps -- simpler than the raw-RPC Solana fallback path.
    """
    txs = await ton_client.get_recent_transactions(trader_address, limit=10)
    if not txs:
        return

    newest_id = txs[0].get("hash") or txs[0].get("lt")
    last_seen = _last_seen_ton.get(trader_address)

    if last_seen is None:
        _last_seen_ton[trader_address] = newest_id
        return

    if newest_id == last_seen:
        return

    new_txs = []
    for tx in txs:
        tx_id = tx.get("hash") or tx.get("lt")
        if tx_id == last_seen:
            break
        new_txs.append(tx)
    _last_seen_ton[trader_address] = newest_id

    for tx in reversed(new_txs):
        event = _parse_ton_tx(trader_address, tx)
        if event:
            await engine.handle_event(event)


def _parse_ton_tx(trader_address: str, tx: dict) -> ChainEvent | None:
    """
    tonapi.io's /v2/blockchain/accounts/{address}/transactions returns
    in_msg/out_msgs; jetton swaps typically surface as a JettonSwap action
    when using the /v2/accounts/{address}/events endpoint instead, which
    gives cleaner "swap" semantics. Swap to that endpoint for production;
    this is a structural placeholder showing the shape.
    """
    actions = tx.get("actions", [])
    for action in actions:
        if action.get("type") != "JettonSwap":
            continue
        swap = action.get("JettonSwap", {})
        jetton_in = swap.get("jetton_master_in")
        jetton_out = swap.get("jetton_master_out")
        amount_in = swap.get("amount_in")
        amount_out = swap.get("amount_out")

        if jetton_out is None and jetton_in:
            # sold jetton_in for TON
            return ChainEvent(
                chain="TON", trader_address=trader_address, side=EventSide.SELL,
                token_address=jetton_in, token_symbol=None,
                usd_value=0.0,  # TODO: price lookup at time of trade
                token_amount=float(amount_in or 0), tx_signature=tx.get("hash", ""),
                occurred_at=datetime.now(timezone.utc),
            )
        elif jetton_out:
            return ChainEvent(
                chain="TON", trader_address=trader_address, side=EventSide.BUY,
                token_address=jetton_out, token_symbol=None,
                usd_value=0.0,
                token_amount=float(amount_out or 0), tx_signature=tx.get("hash", ""),
                occurred_at=datetime.now(timezone.utc),
            )
    return None
