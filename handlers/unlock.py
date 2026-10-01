from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from models.db import Chain, User, Wallet, async_session
from services.crypto_vault import DecryptionError, decrypt_secret
from services.session_keys import DEFAULT_TTL_SECONDS, session_keys
from states import OnboardingStates

router = Router(name="unlock")


@router.callback_query(F.data.startswith("wallet:unlock:"))
async def start_unlock(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[2]
    await state.update_data(unlock_chain=chain)
    await state.set_state(OnboardingStates.awaiting_unlock_pin)
    await callback.message.edit_text(
        f"🔓 Enter your PIN to unlock your {chain} wallet for the next "
        f"{DEFAULT_TTL_SECONDS // 60} minutes:"
    )


@router.message(OnboardingStates.awaiting_unlock_pin)
async def on_unlock_pin(message: Message, state: FSMContext) -> None:
    pin = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    chain = data["unlock_chain"]

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()
        result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.chain == Chain(chain))
        )
        wallet = result.scalar_one_or_none()

    if not wallet:
        await message.answer("Wallet not found.")
        await state.clear()
        return

    try:
        plaintext = decrypt_secret(wallet.encrypted_secret, wallet.secret_salt, pin)
    except DecryptionError:
        await message.answer("❌ Wrong PIN. Try again, or /start to review your setup.")
        return

    session_keys.put(user.id, wallet.id, plaintext)
    del plaintext  # best-effort scrub of this local reference

    await message.answer(
        f"🔓 {chain} wallet unlocked for {DEFAULT_TTL_SECONDS // 60} minutes. "
        "You can now Buy/Sell/Swap without re-entering your PIN until it expires."
    )
    await state.clear()
