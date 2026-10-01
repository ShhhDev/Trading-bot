from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import watchlist_item_kb, watchlist_menu_kb
from models.db import Chain, User, WatchlistItem, async_session
from services.token_metadata import display_label, get_token_metadata
from services.wallet_service import is_valid_address
from states import WatchlistStates

router = Router(name="watchlist")


@router.message(F.text == "👀 Watchlist")
@router.callback_query(F.data == "menu:watchlist")
async def open_watchlist(event: Message | CallbackQuery, state: FSMContext) -> None:
    telegram_id = event.from_user.id
    target = event.message if isinstance(event, CallbackQuery) else event

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            await target.answer("Set up your account first with /start.")
            return
        result = await session.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id))
        items = list(result.scalars().all())

    if not items:
        await target.answer("👀 Your watchlist is empty.", reply_markup=watchlist_menu_kb())
        return

    await target.answer(f"👀 <b>Watchlist</b> ({len(items)} tokens)", reply_markup=watchlist_menu_kb(), parse_mode="HTML")
    for item in items:
        label = item.token_symbol or display_label(item.token_address, None)
        alert_line = f"Alert every ±{item.alert_on_pct_move}%" if item.alert_on_pct_move else "No alert set"
        text = (
            f"<b>{label}</b> ({item.chain.value})\n"
            f"<code>{item.token_address}</code>\n{alert_line}"
        )
        await target.answer(text, reply_markup=watchlist_item_kb(item.chain.value, item.token_address), parse_mode="HTML")


@router.callback_query(F.data == "watch:addnew")
async def add_new_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WatchlistStates.awaiting_ca)
    await callback.message.edit_text("Send the chain + CA, e.g. `SOL <address>` or `TON <address>`:", parse_mode="Markdown")


@router.callback_query(F.data.startswith("watch:add:"))
async def add_from_context(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await _save_watch(callback.from_user.id, chain, token)
    await callback.answer("Added to watchlist ✅")


@router.message(WatchlistStates.awaiting_ca)
async def on_watch_ca(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split()
    if len(parts) != 2 or parts[0].upper() not in ("SOL", "TON"):
        await message.answer("Format: `SOL <address>` or `TON <address>`", parse_mode="Markdown")
        return
    chain, token = parts[0].upper(), parts[1]
    if not is_valid_address(chain, token):
        await message.answer(f"❌ That's not a valid {chain} address. Try again:")
        return
    await _save_watch(message.from_user.id, chain, token)
    await state.clear()
    await message.answer(f"✅ Added {token[:8]} to watchlist. Want price alerts too?", reply_markup=watchlist_item_kb(chain, token))


async def _save_watch(telegram_id: int, chain: str, token: str) -> None:
    meta = await get_token_metadata(chain, token)
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
        existing = await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user.id, WatchlistItem.chain == Chain(chain),
                WatchlistItem.token_address == token,
            )
        )
        if existing.scalar_one_or_none():
            return
        session.add(
            WatchlistItem(
                user_id=user.id, chain=Chain(chain), token_address=token,
                token_symbol=meta.get("symbol"),
            )
        )
        await session.commit()


@router.callback_query(F.data.startswith("watch:setalert:"))
async def start_set_alert(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(alert_chain=chain, alert_token=token)
    await state.set_state(WatchlistStates.awaiting_alert_pct)
    await callback.message.edit_text(
        "Alert me every time price moves ±what %? (e.g. 5 = notify every 5% swing)"
    )


@router.message(WatchlistStates.awaiting_alert_pct)
async def on_alert_pct(message: Message, state: FSMContext) -> None:
    try:
        pct = float(message.text.strip())
    except ValueError:
        await message.answer("Send a number, e.g. 5")
        return

    data = await state.get_data()
    chain, token = data["alert_chain"], data["alert_token"]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()
        result = await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user.id, WatchlistItem.chain == Chain(chain),
                WatchlistItem.token_address == token,
            )
        )
        item = result.scalar_one_or_none()
        if item:
            item.alert_on_pct_move = pct
            await session.commit()

    await message.answer(f"🔔 Alert set: I'll notify you every ±{pct}% move.")
    await state.clear()


@router.callback_query(F.data.startswith("watch:remove:"))
async def remove_watch(callback: CallbackQuery) -> None:
    _, _, chain, token = callback.data.split(":")
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()
        result = await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user.id, WatchlistItem.chain == Chain(chain),
                WatchlistItem.token_address == token,
            )
        )
        item = result.scalar_one_or_none()
        if item:
            await session.delete(item)
            await session.commit()
    await callback.message.edit_text("🗑 Removed from watchlist.")
