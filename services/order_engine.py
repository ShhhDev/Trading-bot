"""
order_engine.py
================
Executes market orders immediately; registers limit/TP/SL orders as PENDING
rows that a scheduler job re-checks against live price every tick and fires
when the trigger condition is met.
"""
from __future__ import annotations

from sqlalchemy import select

from models.db import Order, OrderStatus, OrderType, Wallet, async_session
from services import solana_client as sol_mod
from services import ton_client as ton_mod
from services.crypto_vault import decrypt_secret
from services.guardrails import GuardrailViolation, check_guardrails, log_trade
from services.holdings import get_price_usd
from services.session_keys import session_keys
from services.wallet_service import solana_keypair_from_mnemonic

SOL_MINT = "So11111111111111111111111111111111111111112"
TON_NATIVE = ton_mod.TON_NATIVE_ADDRESS


class InsufficientUnlockError(Exception):
    """Raised when we need the user's PIN but no unlocked session key is cached."""


async def get_signing_material(user_id: int, wallet: Wallet, pin: str | None) -> str:
    """
    Returns decrypted secret (mnemonic/private key) for signing.
    Checks the in-memory session cache first; falls back to decrypting with
    a freshly-provided PIN (and re-caches it briefly).
    """
    cached = session_keys.get(user_id, wallet.id)
    if cached:
        return cached
    if pin is None:
        raise InsufficientUnlockError("Wallet is locked. Please enter your PIN.")
    plaintext = decrypt_secret(wallet.encrypted_secret, wallet.secret_salt, pin)
    session_keys.put(user_id, wallet.id, plaintext)
    return plaintext


async def _estimate_trade_usd_value(chain: str, token_address: str, side: str, amount_in: float) -> float:
    """
    Best-effort USD estimate for guardrail checks BEFORE execution.
    BUY: amount_in is native units (SOL/TON) -> convert via native price.
    SELL: amount_in is token units -> convert via token price.
    Returns 0.0 if a price can't be found (guardrails will then simply not
    block on unpriceable tokens -- logged trades will also show $0 for
    these, which is an acceptable gap for a v1).
    """
    try:
        if side == "BUY":
            if chain == "SOL":
                native_price = await get_price_usd(chain, SOL_MINT)
            elif chain == "TON":
                native_price = await ton_mod.ton_client.get_ton_price_usd()
            else:
                native_price = None
            if native_price is None:
                return 0.0
            return amount_in * native_price
        else:
            token_price = await get_price_usd(chain, token_address)
            if token_price is None:
                return 0.0
            return amount_in * token_price
    except Exception:
        return 0.0


async def execute_market_order(
    user_id: int,
    wallet: Wallet,
    token_address: str,
    side: str,  # "BUY" | "SELL"
    amount_in: float,
    slippage_bps: int,
    pin: str | None = None,
    source: str = "manual",  # manual | copy_trade | limit_order | tp_sl
    skip_guardrails: bool = False,
) -> str:
    """Executes immediately and returns a tx signature. Raises on failure."""
    chain_str = wallet.chain.value
    estimated_usd = await _estimate_trade_usd_value(chain_str, token_address, side, amount_in)

    if not skip_guardrails:
        await check_guardrails(user_id, estimated_usd)

    secret = await get_signing_material(user_id, wallet, pin)

    if wallet.chain == "SOL":
        keypair = solana_keypair_from_mnemonic(secret) if len(secret.split()) in (12, 24) else None
        if keypair is None:
            from solders.keypair import Keypair
            import base58
            keypair = Keypair.from_bytes(base58.b58decode(secret))

        if side == "BUY":
            input_mint, output_mint = SOL_MINT, token_address
            amount_lamports = int(amount_in * sol_mod.LAMPORTS_PER_SOL)
        else:
            input_mint, output_mint = token_address, SOL_MINT
            amount_lamports = int(amount_in)  # caller passes raw token units for sells

        quote = await sol_mod.solana_client.get_jupiter_quote(
            input_mint, output_mint, amount_lamports, slippage_bps
        )
        tx_sig = await sol_mod.solana_client.execute_jupiter_swap(quote, keypair)
        await log_trade(user_id, "SOL", token_address, side, estimated_usd, tx_sig, source=source)
        return tx_sig

    elif wallet.chain == "TON":
        # amount_in for BUY is in native TON (human units); for SELL it's in
        # jetton units (human units). STON.fi's API wants raw integer units
        # (i.e. multiplied by decimals) -- 9 decimals for native TON, and
        # we look up the jetton's own decimals for sells.
        TON_DECIMALS = 9

        if side == "BUY":
            offer_address = TON_NATIVE
            ask_address = token_address
            offer_units = int(amount_in * (10**TON_DECIMALS))
        else:
            offer_address = token_address
            ask_address = TON_NATIVE
            jetton_decimals = await _get_jetton_decimals(wallet.public_address, token_address)
            offer_units = int(amount_in * (10**jetton_decimals))

        quote = await ton_mod.ton_client.get_swap_quote(
            offer_address, ask_address, offer_units, slippage_bps
        )
        tx_hash = await ton_mod.ton_client.execute_swap(quote, secret)
        await log_trade(user_id, "TON", token_address, side, estimated_usd, tx_hash, source=source)
        return tx_hash

    raise ValueError(f"Unsupported chain: {wallet.chain}")


async def _get_jetton_decimals(owner_address: str, jetton_address: str) -> int:
    """Looks up a jetton's decimals from the owner's holdings (defaults to 9 if not found)."""
    holdings = await ton_mod.ton_client.get_jetton_holdings(owner_address)
    match = next((h for h in holdings if h.jetton_address == jetton_address), None)
    return match.decimals if match else 9


async def place_conditional_order(
    user_id: int,
    wallet_id: int,
    chain: str,
    token_address: str,
    order_type: OrderType,
    amount_in: float,
    slippage_bps: int,
    trigger_price_usd: float | None = None,
    trigger_pct_change: float | None = None,
    reference_price_usd: float | None = None,
) -> Order:
    """Registers a limit/TP/SL order for the background checker to pick up."""
    async with async_session() as session:
        order = Order(
            user_id=user_id,
            wallet_id=wallet_id,
            chain=chain,
            order_type=order_type,
            status=OrderStatus.PENDING,
            token_address=token_address,
            amount_in=amount_in,
            slippage_bps=slippage_bps,
            trigger_price_usd=trigger_price_usd,
            trigger_pct_change=trigger_pct_change,
            reference_price_usd=reference_price_usd,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


async def check_pending_orders(notifier) -> None:
    """
    Scheduler tick: re-check every PENDING conditional order's trigger against
    live price; execute + notify on fire. Call every ~5-15s from main.py.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.status == OrderStatus.PENDING)
        )
        pending = list(result.scalars().all())

    for order in pending:
        try:
            current_price = await get_price_usd(order.chain, order.token_address)
            if current_price is None:
                continue
            if not _is_triggered(order, current_price):
                continue

            async with async_session() as session:
                wallet = await session.get(Wallet, order.wallet_id)

            side = "BUY" if order.order_type in (OrderType.LIMIT_BUY,) else "SELL"
            order_source = "limit_order" if order.order_type in (OrderType.LIMIT_BUY, OrderType.LIMIT_SELL) else "tp_sl"
            tx_sig = await execute_market_order(
                order.user_id, wallet, order.token_address, side,
                order.amount_in, order.slippage_bps, source=order_source,
            )

            async with async_session() as session:
                db_order = await session.get(Order, order.id)
                db_order.status = OrderStatus.FILLED
                db_order.tx_signature = tx_sig
                db_order.filled_price_usd = current_price
                await session.commit()

            await notifier(
                order.user_id,
                f"✅ {order.order_type.value} filled for "
                f"{order.token_symbol or order.token_address[:6]} at ${current_price:.6f}\n"
                f"Tx: {tx_sig}",
            )
        except Exception as e:  # noqa: BLE001
            async with async_session() as session:
                db_order = await session.get(Order, order.id)
                db_order.status = OrderStatus.FAILED
                db_order.error_message = str(e)[:250]
                await session.commit()
            await notifier(order.user_id, f"⚠️ Order failed: {e}")


def _is_triggered(order: Order, current_price: float) -> bool:
    if order.trigger_price_usd is not None:
        if order.order_type == OrderType.LIMIT_BUY:
            return current_price <= order.trigger_price_usd
        if order.order_type == OrderType.LIMIT_SELL:
            return current_price >= order.trigger_price_usd
        if order.order_type == OrderType.TAKE_PROFIT:
            return current_price >= order.trigger_price_usd
        if order.order_type == OrderType.STOP_LOSS:
            return current_price <= order.trigger_price_usd

    if order.trigger_pct_change is not None and order.reference_price_usd:
        pct_move = ((current_price - order.reference_price_usd) / order.reference_price_usd) * 100
        if order.order_type == OrderType.TAKE_PROFIT:
            return pct_move >= order.trigger_pct_change
        if order.order_type == OrderType.STOP_LOSS:
            return pct_move <= -abs(order.trigger_pct_change)

    return False
