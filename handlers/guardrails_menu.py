from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import guardrails_menu_kb
from models.db import User, async_session
from states import TradeStates

router = Router(name="guardrails_menu")

FIELD_LABELS = {
    "max_trade": ("max_trade_usd", "Max $ allowed in a single trade (send 0 to remove this limit):"),
    "max_daily": ("max_daily_volume_usd", "Max total $ volume allowed per rolling 24h (send 0 to remove):"),
    "confirm_above": ("require_confirm_above_usd", "Ask for extra confirmation above this $ amount (send 0 to always confirm, or a large number to disable):"),
}


@router.callback_query(F.data == "wallet:guardrails")
async def open_guardrails(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Set up your account first with /start.", show_alert=True)
        return

    def fmt(v: float | None) -> str:
        return f"${v:,.0f}" if v else "No limit"

    text = (
        "🛡 <b>Trade Limits</b>\n\n"
        f"Max per-trade: {fmt(user.max_trade_usd)}\n"
        f"Max daily volume: {fmt(user.max_daily_volume_usd)}\n"
        f"Confirm above: {fmt(user.require_confirm_above_usd)}\n\n"
        "These protect against fat-finger mistakes and, for daily volume, "
        "runaway auto-copy activity. They apply to ALL trades — manual, "
        "limit/TP-SL fills, and copy-trade auto-copy."
    )
    await callback.message.edit_text(text, reply_markup=guardrails_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data.startswith("guardrail:set:"))
async def start_set_guardrail(callback: CallbackQuery, state: FSMContext) -> None:
    field_key = callback.data.split(":")[2]
    if field_key not in FIELD_LABELS:
        await callback.answer("Unknown setting", show_alert=True)
        return
    await state.update_data(guardrail_field=field_key)
    await state.set_state(TradeStates.awaiting_guardrail_value)
    _, prompt = FIELD_LABELS[field_key]
    await callback.message.edit_text(prompt)


@router.message(TradeStates.awaiting_guardrail_value)
async def on_guardrail_value(message: Message, state: FSMContext) -> None:
    try:
        value = float(message.text.strip())
    except ValueError:
        await message.answer("Send a number, e.g. 100 or 0 to remove the limit.")
        return

    data = await state.get_data()
    field_key = data["guardrail_field"]
    db_field, _ = FIELD_LABELS[field_key]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()
        setattr(user, db_field, None if value <= 0 else value)
        await session.commit()

    await message.answer(
        f"✅ Updated. {'Limit removed.' if value <= 0 else f'Set to ${value:,.0f}.'}",
        reply_markup=guardrails_menu_kb(),
    )
    await state.clear()


@router.callback_query(F.data == "guardrail:clear")
async def clear_guardrails(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()
        user.max_trade_usd = None
        user.max_daily_volume_usd = None
        user.require_confirm_above_usd = None
        await session.commit()
    await callback.answer("All limits cleared ✅")
    await open_guardrails(callback)
