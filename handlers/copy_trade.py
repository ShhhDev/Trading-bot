from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import (
    copytrade_amount_mode_kb, copytrade_item_kb, copytrade_menu_kb, copytrade_mode_kb,
)
from models.db import Chain, CopyMode, CopyTradeConfig, User, async_session
from services.wallet_service import is_valid_address
from states import CopyTradeStates

router = Router(name="copy_trade")


@router.message(F.text == "🔁 Copy Trade")
@router.callback_query(F.data == "menu:copytrade")
async def open_copytrade_menu(event: Message | CallbackQuery, state: FSMContext) -> None:
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(
        "🔁 <b>Copy Trading</b>\n\n"
        "Follow any wallet on Solana or TON. Get notified on their trades, "
        "or auto-copy them with your own sizing rules.\n\n"
        "You can add multiple trader wallets — each with its own settings.",
        reply_markup=copytrade_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "copytrade:add")
async def add_trader_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CopyTradeStates.awaiting_trader_address)
    await callback.message.edit_text(
        "Send the chain + wallet address to follow, e.g.:\n`SOL <address>` or `TON <address>`",
        parse_mode="Markdown",
    )


@router.message(CopyTradeStates.awaiting_trader_address)
async def on_trader_address(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split()
    if len(parts) != 2 or parts[0].upper() not in ("SOL", "TON"):
        await message.answer("Format: `SOL <address>` or `TON <address>`", parse_mode="Markdown")
        return
    chain, address = parts[0].upper(), parts[1]
    if not is_valid_address(chain, address):
        await message.answer(f"❌ That's not a valid {chain} address. Try again:")
        return
    await state.update_data(ct_chain=chain, ct_address=address)
    await state.set_state(CopyTradeStates.awaiting_label)
    await message.answer("Give this trader a nickname (or send `-` to skip):")


@router.message(CopyTradeStates.awaiting_label)
async def on_trader_label(message: Message, state: FSMContext) -> None:
    label = message.text.strip()
    label = None if label == "-" else label
    await state.update_data(ct_label=label)
    await state.set_state(CopyTradeStates.choosing_mode)
    await message.answer("How should I handle this trader's activity?", reply_markup=copytrade_mode_kb())


@router.callback_query(F.data.startswith("copytrade:mode:"))
async def on_mode_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":")[2]  # notify | auto
    await state.update_data(ct_mode=mode)

    if mode == "notify":
        await state.set_state(CopyTradeStates.awaiting_min_buy_usd)
        await callback.message.edit_text(
            "Minimum trade size (USD) before I notify you? (send 0 for all trades)"
        )
    else:
        await state.set_state(CopyTradeStates.awaiting_min_buy_usd)
        await callback.message.edit_text(
            "Minimum trade size (USD) the trader must hit before I copy it? (send 0 for all trades)"
        )


@router.message(CopyTradeStates.awaiting_min_buy_usd)
async def on_min_buy(message: Message, state: FSMContext) -> None:
    try:
        min_usd = float(message.text.strip())
    except ValueError:
        await message.answer("Send a number, e.g. 500")
        return
    await state.update_data(ct_min_buy=min_usd)

    data = await state.get_data()
    if data["ct_mode"] == "notify":
        await finalize_copytrade(message, state)
        return

    await state.set_state(CopyTradeStates.awaiting_copy_amount)
    await message.answer(
        "How do you want to size your copy trades?", reply_markup=copytrade_amount_mode_kb()
    )


@router.callback_query(F.data.startswith("copytrade:amtmode:"))
async def on_amount_mode(callback: CallbackQuery, state: FSMContext) -> None:
    amt_mode = callback.data.split(":")[2]  # fixed | pct
    await state.update_data(ct_amt_mode=amt_mode)
    await state.set_state(CopyTradeStates.awaiting_copy_amount)
    if amt_mode == "fixed":
        await callback.message.edit_text("Fixed $ amount to spend per copied trade (e.g. 50):")
    else:
        await callback.message.edit_text("What % of the trader's trade size should you mirror? (e.g. 10):")


@router.message(CopyTradeStates.awaiting_copy_amount)
async def on_copy_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.strip())
    except ValueError:
        await message.answer("Send a valid number.")
        return
    await state.update_data(ct_copy_amount=amount)
    await state.set_state(CopyTradeStates.awaiting_slippage)
    await message.answer("Max slippage % allowed for auto-copied trades (e.g. 3):")


@router.message(CopyTradeStates.awaiting_slippage)
async def on_ct_slippage(message: Message, state: FSMContext) -> None:
    try:
        slippage_pct = float(message.text.strip())
    except ValueError:
        await message.answer("Send a valid number.")
        return
    await state.update_data(ct_slippage_bps=int(slippage_pct * 100))
    await finalize_copytrade(message, state)


async def finalize_copytrade(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chain = data["ct_chain"]
    address = data["ct_address"]
    label = data.get("ct_label")
    mode = CopyMode.AUTO_COPY if data["ct_mode"] == "auto" else CopyMode.NOTIFY_ONLY
    min_buy = data["ct_min_buy"]
    copy_amount = data.get("ct_copy_amount", 0.0)
    amt_mode = data.get("ct_amt_mode", "fixed")
    slippage_bps = data.get("ct_slippage_bps", 300)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()

        cfg = CopyTradeConfig(
            user_id=user.id, chain=Chain(chain), trader_address=address, label=label,
            mode=mode, min_trader_buy_usd=min_buy, copy_amount_usd=copy_amount,
            copy_amount_is_pct_of_trader=(amt_mode == "pct"), max_slippage_bps=slippage_bps,
        )
        session.add(cfg)
        await session.commit()

    mode_desc = "🔔 Notify only" if mode == CopyMode.NOTIFY_ONLY else "🔁 Auto-copy"
    await message.answer(
        f"✅ Now following <b>{label or address[:8]}</b> on {chain}\n"
        f"Mode: {mode_desc}\nMin trade size: ${min_buy}\n"
        f"{'Copy amount: $' + str(copy_amount) if mode == CopyMode.AUTO_COPY and amt_mode == 'fixed' else ''}"
        f"{'Copy at: ' + str(copy_amount) + '% of their size' if mode == CopyMode.AUTO_COPY and amt_mode == 'pct' else ''}",
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(F.data == "copytrade:list")
async def list_configs(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()
        result = await session.execute(select(CopyTradeConfig).where(CopyTradeConfig.user_id == user.id))
        configs = list(result.scalars().all())

    if not configs:
        await callback.message.answer("You're not following any traders yet.")
        return

    for cfg in configs:
        status = "🟢 Active" if cfg.is_active else "⏸ Paused"
        mode_desc = "🔔 Notify only" if cfg.mode == CopyMode.NOTIFY_ONLY else "🔁 Auto-copy"
        text = (
            f"<b>{cfg.label or cfg.trader_address[:8]}</b> ({cfg.chain.value}) — {status}\n"
            f"<code>{cfg.trader_address}</code>\n"
            f"{mode_desc} | Min: ${cfg.min_trader_buy_usd} | Slippage: {cfg.max_slippage_bps/100}%"
        )
        await callback.message.answer(
            text, reply_markup=copytrade_item_kb(cfg.id, cfg.is_active, cfg.mode.value), parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("copytrade:toggle:"))
async def toggle_config(callback: CallbackQuery) -> None:
    cfg_id = int(callback.data.split(":")[2])
    async with async_session() as session:
        from models.db import CopyTradeConfig as CTC
        cfg = await session.get(CTC, cfg_id)
        if cfg:
            cfg.is_active = not cfg.is_active
            await session.commit()
            await callback.answer("Toggled ✅")
            await callback.message.edit_reply_markup(
                reply_markup=copytrade_item_kb(cfg.id, cfg.is_active, cfg.mode.value)
            )


@router.callback_query(F.data.startswith("copytrade:switchmode:"))
async def switch_mode(callback: CallbackQuery) -> None:
    cfg_id = int(callback.data.split(":")[2])
    async with async_session() as session:
        from models.db import CopyTradeConfig as CTC
        cfg = await session.get(CTC, cfg_id)
        if cfg:
            cfg.mode = CopyMode.AUTO_COPY if cfg.mode == CopyMode.NOTIFY_ONLY else CopyMode.NOTIFY_ONLY
            await session.commit()
            await callback.answer(f"Switched to {cfg.mode.value} ✅")
            await callback.message.edit_reply_markup(
                reply_markup=copytrade_item_kb(cfg.id, cfg.is_active, cfg.mode.value)
            )


@router.callback_query(F.data.startswith("copytrade:remove:"))
async def remove_config(callback: CallbackQuery) -> None:
    cfg_id = int(callback.data.split(":")[2])
    async with async_session() as session:
        from models.db import CopyTradeConfig as CTC
        cfg = await session.get(CTC, cfg_id)
        if cfg:
            await session.delete(cfg)
            await session.commit()
    await callback.message.edit_text("🗑 Removed from copy trade list.")
