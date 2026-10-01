from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import (
    buy_amount_presets_kb, confirm_large_buy_kb, order_type_kb, post_buy_kb, sell_pct_kb,
)
from models.db import Chain, OrderType, User, Wallet, async_session
from services.guardrails import GuardrailViolation, needs_extra_confirmation
from services.holdings import get_holding_amount, get_price_usd
from services.order_engine import (
    InsufficientUnlockError, execute_market_order, place_conditional_order,
)
from services.wallet_service import is_valid_address
from states import TradeStates

router = Router(name="trading")


async def _get_wallet(telegram_id: int, chain: str) -> Wallet | None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.chain == Chain(chain))
        )
        return result.scalar_one_or_none()


# ------------------------------------------------------------------ Buy -----

@router.callback_query(F.data.startswith("trade:buy:"))
async def start_buy(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    chain = parts[2]
    token_address = parts[3] if len(parts) > 3 else None

    if not token_address:
        await state.set_state(TradeStates.awaiting_ca_for_buy)
        await state.update_data(buy_chain=chain)
        await callback.message.edit_text(
            f"Paste the contract address (CA) you want to buy on {chain}:"
        )
        return

    await state.update_data(buy_chain=chain, buy_token=token_address)
    await callback.message.edit_text(
        f"Buying <code>{token_address}</code>\n\nChoose amount:",
        reply_markup=buy_amount_presets_kb(chain),
        parse_mode="HTML",
    )


@router.message(TradeStates.awaiting_ca_for_buy)
async def on_buy_ca_received(message: Message, state: FSMContext) -> None:
    ca = (message.text or "").strip()
    data = await state.get_data()
    chain = data["buy_chain"]

    if not is_valid_address(chain, ca):
        await message.answer(
            f"❌ That doesn't look like a valid {chain} address. Double-check the CA and try again:"
        )
        return

    price = await get_price_usd(chain, ca)
    if price is None:
        await message.answer(
            "⚠️ That address looks valid but I can't find price/liquidity data for it — "
            "it may not be tradeable, or the address is wrong. Send a different CA, or "
            "tap an amount below to try anyway.",
            reply_markup=buy_amount_presets_kb(chain),
        )
        await state.update_data(buy_token=ca)
        return

    await state.update_data(buy_token=ca)
    await message.answer(
        f"Buying <code>{ca}</code>\nCurrent price: ${price:.8f}\n\nChoose amount:",
        reply_markup=buy_amount_presets_kb(chain),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "trade:customamount")
async def ask_custom_buy_amount(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TradeStates.awaiting_buy_amount)
    await callback.message.edit_text("Enter the amount to spend (e.g. 2.5):")


@router.message(TradeStates.awaiting_buy_amount)
async def on_custom_buy_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.strip())
    except ValueError:
        await message.answer("Please send a valid number.")
        return
    await execute_buy(message, state, amount)


@router.callback_query(F.data.startswith("trade:amt:"))
async def on_preset_buy_amount(callback: CallbackQuery, state: FSMContext) -> None:
    amount = float(callback.data.split(":")[2])
    await execute_buy(callback.message, state, amount, callback=callback)


async def execute_buy(
    target: Message, state: FSMContext, amount: float, callback: CallbackQuery | None = None,
    skip_confirm: bool = False,
) -> None:
    data = await state.get_data()
    chain = data["buy_chain"]
    token = data["buy_token"]
    telegram_id = callback.from_user.id if callback else target.chat.id

    wallet = await _get_wallet(telegram_id, chain)
    if not wallet:
        await target.answer(f"No {chain} wallet found.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
        slippage_bps = user.default_slippage_bps

    # Soft confirmation gate for large manual buys (not applied to limit/TP-SL
    # fills or copy-trade auto-copy, which are pre-authorized when configured).
    if not skip_confirm:
        price = await get_price_usd(chain, token)
        native_price = await get_price_usd(chain, "So11111111111111111111111111111111111111112") if chain == "SOL" else None
        estimated_usd = amount * native_price if native_price else None

        if estimated_usd and await needs_extra_confirmation(user.id, estimated_usd):
            await state.update_data(buy_amount_pending=amount)
            await target.answer(
                f"⚠️ This buy is ~${estimated_usd:,.2f} — above your confirmation threshold.\n\n"
                f"Buying {token[:8]}... for {amount} {chain}. Confirm?",
                reply_markup=confirm_large_buy_kb(chain),
            )
            return

    status_msg = await target.answer(f"⏳ Buying {token[:8]}... for {amount} {chain}...")
    try:
        tx_sig = await execute_market_order(
            user_id=user.id, wallet=wallet, token_address=token, side="BUY",
            amount_in=amount, slippage_bps=slippage_bps,
        )
        await status_msg.edit_text(
            f"✅ Bought! \nTx: <code>{tx_sig}</code>",
            reply_markup=post_buy_kb(token, chain),
            parse_mode="HTML",
        )
    except InsufficientUnlockError:
        await status_msg.edit_text(
            "🔒 Your wallet is locked. Tap 'Unlock wallet (PIN)' in the wallet menu first, "
            "then retry this trade."
        )
    except GuardrailViolation as e:
        await status_msg.edit_text(f"🛑 {e}")
    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"❌ Trade failed: {e}")

    await state.clear()


@router.callback_query(F.data == "trade:confirmbuy")
async def on_confirm_large_buy(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    amount = data.get("buy_amount_pending")
    if amount is None:
        await callback.answer("This confirmation expired. Please start the buy again.", show_alert=True)
        return
    await callback.message.edit_text("⏳ Confirmed, executing...")
    await execute_buy(callback.message, state, amount, callback=callback, skip_confirm=True)


# ----------------------------------------------------------------- Sell -----

@router.callback_query(F.data.startswith("trade:sell:"))
async def start_sell(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    chain = parts[2]
    token_address = parts[3] if len(parts) > 3 else None
    if not token_address:
        await callback.message.edit_text("Paste the CA of the token you want to sell:")
        await state.set_state(TradeStates.awaiting_ca_for_sell)
        await state.update_data(sell_chain=chain)
        return

    await state.update_data(sell_chain=chain, sell_token=token_address)
    await callback.message.edit_text(
        f"Selling <code>{token_address}</code>\n\nHow much of your holding?",
        reply_markup=sell_pct_kb(),
        parse_mode="HTML",
    )


@router.message(TradeStates.awaiting_ca_for_sell)
async def on_sell_ca_received(message: Message, state: FSMContext) -> None:
    ca = (message.text or "").strip()
    data = await state.get_data()
    chain = data["sell_chain"]

    if not is_valid_address(chain, ca):
        await message.answer(
            f"❌ That doesn't look like a valid {chain} address. Double-check the CA and try again:"
        )
        return

    wallet = await _get_wallet(message.from_user.id, chain)
    if wallet:
        held = await get_holding_amount(chain, wallet.public_address, ca)
        if held <= 0:
            await message.answer(
                "You don't currently hold this token in your wallet, so there's nothing to sell. "
                "Send a different CA, or /start over."
            )
            return

    await state.update_data(sell_token=ca)
    await message.answer(
        f"Selling <code>{ca}</code>\n\nHow much of your holding?",
        reply_markup=sell_pct_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("trade:sellpct:"))
async def on_sell_pct(callback: CallbackQuery, state: FSMContext) -> None:
    pct_raw = callback.data.split(":")[2]
    if pct_raw == "custom":
        await state.set_state(TradeStates.awaiting_sell_pct)
        await callback.message.edit_text("Enter % of your holding to sell (1-100):")
        return

    await execute_sell(callback.message, state, int(pct_raw), callback=callback)


@router.message(TradeStates.awaiting_sell_pct)
async def on_custom_sell_pct(message: Message, state: FSMContext) -> None:
    try:
        pct = float(message.text.strip())
    except ValueError:
        await message.answer("Please send a number between 1 and 100.")
        return
    if not (0 < pct <= 100):
        await message.answer("Enter a value between 1 and 100.")
        return
    await execute_sell(message, state, pct)


async def execute_sell(
    target: Message, state: FSMContext, pct: float, callback: CallbackQuery | None = None
) -> None:
    data = await state.get_data()
    chain = data["sell_chain"]
    token = data["sell_token"]
    telegram_id = callback.from_user.id if callback else target.chat.id

    wallet = await _get_wallet(telegram_id, chain)
    if not wallet:
        await target.answer("Wallet not found.")
        return

    held_amount = await get_holding_amount(chain, wallet.public_address, token)
    if held_amount <= 0:
        await target.answer("No holding found for this token in your wallet.")
        await state.clear()
        return

    sell_amount = held_amount * (pct / 100.0)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()

    status_msg = await target.answer(f"⏳ Selling {pct:g}% ({sell_amount:.4f})...")
    try:
        tx_sig = await execute_market_order(
            user_id=user.id, wallet=wallet, token_address=token, side="SELL",
            amount_in=sell_amount, slippage_bps=user.default_slippage_bps,
        )
        await status_msg.edit_text(f"✅ Sold! Tx: <code>{tx_sig}</code>", parse_mode="HTML")
    except InsufficientUnlockError:
        await status_msg.edit_text("🔒 Wallet locked. Unlock with your PIN first, then retry.")
    except GuardrailViolation as e:
        await status_msg.edit_text(f"🛑 {e}")
    except Exception as e:  # noqa: BLE001
        await status_msg.edit_text(f"❌ Sell failed: {e}")

    await state.clear()


# ---------------------------------------------------- Limit / TP / SL -----

@router.callback_query(F.data.startswith("order:tp:"))
async def start_take_profit(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(order_chain=chain, order_token=token, order_kind="tp")
    await state.set_state(TradeStates.awaiting_tp_pct)
    await callback.message.edit_text(
        "🎯 Set Take Profit: sell automatically when price is up what %? (e.g. 50 for +50%)"
    )


@router.callback_query(F.data.startswith("order:sl:"))
async def start_stop_loss(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(order_chain=chain, order_token=token, order_kind="sl")
    await state.set_state(TradeStates.awaiting_sl_pct)
    await callback.message.edit_text(
        "🛑 Set Stop Loss: sell automatically when price is down what %? (e.g. 20 for -20%)"
    )


@router.message(TradeStates.awaiting_tp_pct)
@router.message(TradeStates.awaiting_sl_pct)
async def on_tp_sl_pct(message: Message, state: FSMContext) -> None:
    try:
        pct = float(message.text.strip())
    except ValueError:
        await message.answer("Send a number, e.g. 25")
        return
    if pct <= 0:
        await message.answer("Enter a positive number, e.g. 25")
        return

    data = await state.get_data()
    chain = data["order_chain"]
    token = data["order_token"]

    wallet = await _get_wallet(message.from_user.id, chain)
    if not wallet:
        await message.answer("Wallet not found.")
        await state.clear()
        return

    held_amount = await get_holding_amount(chain, wallet.public_address, token)
    if held_amount <= 0:
        await message.answer(
            "You don't currently hold this token in your wallet, so there's nothing to "
            "protect with a TP/SL order. Buy it first, then set this up."
        )
        await state.clear()
        return

    await state.update_data(order_trigger_pct=pct, order_held_amount=held_amount)
    await state.set_state(TradeStates.awaiting_tp_sl_sell_pct)
    await message.answer(
        f"You hold {held_amount:,.4f} of this token. "
        "What % of that holding should this order sell when triggered? "
        "(e.g. 100 = sell it all, 50 = sell half):"
    )


@router.message(TradeStates.awaiting_tp_sl_sell_pct)
async def on_tp_sl_sell_pct(message: Message, state: FSMContext) -> None:
    try:
        sell_pct = float(message.text.strip())
    except ValueError:
        await message.answer("Send a number between 1 and 100.")
        return
    if not (0 < sell_pct <= 100):
        await message.answer("Enter a value between 1 and 100.")
        return

    data = await state.get_data()
    chain = data["order_chain"]
    token = data["order_token"]
    kind = data["order_kind"]
    trigger_pct = data["order_trigger_pct"]
    held_amount = data["order_held_amount"]

    wallet = await _get_wallet(message.from_user.id, chain)
    if not wallet:
        await message.answer("Wallet not found.")
        await state.clear()
        return

    ref_price = await get_price_usd(chain, token)
    if ref_price is None:
        await message.answer("Couldn't fetch a current price for this token. Try again shortly.")
        await state.clear()
        return

    sell_amount = held_amount * (sell_pct / 100.0)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()

    order_type = OrderType.TAKE_PROFIT if kind == "tp" else OrderType.STOP_LOSS
    await place_conditional_order(
        user_id=user.id, wallet_id=wallet.id, chain=chain, token_address=token,
        order_type=order_type, amount_in=sell_amount,
        slippage_bps=user.default_slippage_bps,
        trigger_pct_change=trigger_pct, reference_price_usd=ref_price,
    )

    label = "Take Profit" if kind == "tp" else "Stop Loss"
    await message.answer(
        f"✅ {label} order set: sell {sell_pct:g}% of your holding "
        f"({sell_amount:.4f} tokens) if price moves "
        f"{'+' if kind == 'tp' else '-'}{trigger_pct}% from ${ref_price:.6f}.\n\n"
        "⚠️ Note: your holding size is locked in at order-creation time. If you buy "
        "or sell more of this token before this order fires, the sell amount won't "
        "auto-adjust — cancel and re-create the order if your position size changes."
    )
    await state.clear()


@router.callback_query(F.data.startswith("order:limitbuy:"))
async def start_limit_buy(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(order_chain=chain, order_token=token, order_kind="limitbuy")
    await state.set_state(TradeStates.awaiting_limit_price)
    await callback.message.edit_text("Enter target price in USD to buy at (e.g. 0.0042):")


@router.callback_query(F.data.startswith("order:limitsell:"))
async def start_limit_sell(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(order_chain=chain, order_token=token, order_kind="limitsell")
    await state.set_state(TradeStates.awaiting_limit_price)
    await callback.message.edit_text("Enter target price in USD to sell at (e.g. 0.0080):")


@router.message(TradeStates.awaiting_limit_price)
async def on_limit_price(message: Message, state: FSMContext) -> None:
    try:
        price = float(message.text.strip())
    except ValueError:
        await message.answer("Send a valid price, e.g. 0.0042")
        return
    await state.update_data(limit_price=price)
    await state.set_state(TradeStates.awaiting_limit_amount)
    await message.answer("Enter amount to trade at that price:")


@router.message(TradeStates.awaiting_limit_amount)
async def on_limit_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.strip())
    except ValueError:
        await message.answer("Send a valid amount.")
        return

    data = await state.get_data()
    chain = data["order_chain"]
    token = data["order_token"]
    kind = data["order_kind"]
    price = data["limit_price"]

    wallet = await _get_wallet(message.from_user.id, chain)
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()

    order_type = OrderType.LIMIT_BUY if kind == "limitbuy" else OrderType.LIMIT_SELL
    await place_conditional_order(
        user_id=user.id, wallet_id=wallet.id, chain=chain, token_address=token,
        order_type=order_type, amount_in=amount, slippage_bps=user.default_slippage_bps,
        trigger_price_usd=price,
    )
    await message.answer(
        f"✅ Limit order set: {kind.replace('limit', '').upper()} {amount} at ${price}. "
        "I'll notify you when it fills."
    )
    await state.clear()


@router.callback_query(F.data.startswith("orders:list:"))
async def list_orders(callback: CallbackQuery) -> None:
    chain = callback.data.split(":")[2]
    telegram_id = callback.from_user.id
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
        from models.db import Order, OrderStatus
        result = await session.execute(
            select(Order).where(
                Order.user_id == user.id, Order.chain == Chain(chain),
                Order.status == OrderStatus.PENDING,
            )
        )
        orders = list(result.scalars().all())

    if not orders:
        await callback.message.answer("No pending orders.")
        return

    lines = []
    for o in orders:
        trigger = f"${o.trigger_price_usd}" if o.trigger_price_usd else f"{o.trigger_pct_change:+.1f}%"
        lines.append(f"• {o.order_type.value} {o.token_address[:8]} @ {trigger}")
    await callback.message.answer("<b>Pending Orders</b>\n\n" + "\n".join(lines), parse_mode="HTML")
