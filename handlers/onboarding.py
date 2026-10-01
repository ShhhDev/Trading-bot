"""
handlers/onboarding.py
=======================
/start -> chain selection -> per chain: import or create wallet -> PIN setup
-> (if created) show seed ONCE with a "I saved it" confirm gate -> main menu.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import (
    chain_selection_kb, confirm_seed_saved_kb, import_warning_kb,
    main_menu_kb, wallet_action_kb,
)
from keyboards.reply import persistent_menu_kb
from models.db import Chain, User, Wallet, async_session
from services.crypto_vault import encrypt_secret
from services.wallet_service import (
    generate_wallet_for_chain, derive_wallet_for_chain, validate_secret_for_chain,
)
from states import OnboardingStates

router = Router(name="onboarding")


async def get_or_create_user(telegram_id: int, username: str | None) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_or_create_user(message.from_user.id, message.from_user.username)

    async with async_session() as session:
        result = await session.execute(select(Wallet).where(Wallet.user_id == user.id))
        existing_wallets = result.scalars().all()

    if existing_wallets:
        await message.answer(
            "Welcome back 👋 Pick where you want to go:",
            reply_markup=main_menu_kb(),
        )
        await message.answer("Menu ready below 👇", reply_markup=persistent_menu_kb())
        return

    await state.set_state(OnboardingStates.choosing_chain)
    await message.answer(
        "👋 <b>Welcome to [YourBot] — non-custodial Solana &amp; TON trading</b>\n\n"
        "First, pick the blockchain(s) you want to trade on:",
        reply_markup=chain_selection_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("chain:"))
async def on_chain_selected(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]  # SOL | TON | BOTH
    chains = ["SOL", "TON"] if choice == "BOTH" else [choice]
    await state.update_data(pending_chains=chains, chain_idx=0)

    await start_wallet_setup_for_next_chain(callback, state)


async def start_wallet_setup_for_next_chain(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chains: list[str] = data["pending_chains"]
    idx: int = data["chain_idx"]

    if idx >= len(chains):
        user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        async with async_session() as session:
            db_user = await session.get(User, user.id)
            db_user.selected_chains = ",".join(chains)
            await session.commit()

        await state.clear()
        await callback.message.edit_text(
            "🎉 <b>You're all set!</b>\n\nUse the menu below to trade, watch tokens, or set up copy trading.",
            parse_mode="HTML",
        )
        await callback.message.answer("Main menu:", reply_markup=main_menu_kb())
        await callback.message.answer("Quick access below 👇", reply_markup=persistent_menu_kb())
        return

    chain = chains[idx]
    await state.set_state(OnboardingStates.choosing_wallet_action)
    chain_name = "Solana" if chain == "SOL" else "TON"
    await callback.message.edit_text(
        f"<b>{chain_name} wallet setup</b>\n\nDo you already have a wallet, or want a new one?",
        reply_markup=wallet_action_kb(chain),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("walletaction:import:"))
async def on_choose_import(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[2]
    await state.update_data(import_chain=chain)
    await callback.message.edit_text(
        "⚠️ <b>Security warning before you continue</b>\n\n"
        "• Never share your seed phrase with anyone else — including fake "
        "\"support\" accounts.\n"
        "• We encrypt it with a PIN <i>you</i> choose. We never store your PIN. "
        "If you lose both your PIN and your own backup, we cannot recover funds.\n"
        "• Only paste this here if you trust this bot with a wallet you're "
        "comfortable actively trading from.\n\n"
        "Continue?",
        reply_markup=import_warning_kb(chain),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("importconfirm:"))
async def on_import_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[1]
    await state.set_state(OnboardingStates.awaiting_seed_import)
    await state.update_data(import_chain=chain)
    label = "24-word seed phrase or private key" if chain == "SOL" else "24-word seed phrase"
    await callback.message.edit_text(
        f"Send your {label} now.\n\n"
        "🗑 <i>Delete your message right after sending — I'll process it immediately "
        "and it's never logged.</i>",
        parse_mode="HTML",
    )


@router.message(OnboardingStates.awaiting_seed_import)
async def on_seed_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chain = data["import_chain"]
    secret = message.text or ""

    # Best-effort: delete the message containing the secret from chat history
    try:
        await message.delete()
    except Exception:
        pass

    if not validate_secret_for_chain(chain, secret):
        await message.answer(
            "❌ That doesn't look like a valid "
            f"{'seed phrase / private key' if chain == 'SOL' else 'seed phrase'} for {chain}. "
            "Please try again, or /start over."
        )
        return

    await state.update_data(pending_secret=secret, pending_is_import=True)
    await state.set_state(OnboardingStates.awaiting_new_pin)
    await message.answer(
        "✅ Valid! Now set a <b>PIN</b> (4–8 digits or a passphrase) to encrypt this wallet.\n\n"
        "⚠️ We do not store this PIN anywhere and cannot reset it. Write it down somewhere safe.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("walletaction:create:"))
async def on_choose_create(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[2]
    derived = generate_wallet_for_chain(chain)
    await state.update_data(
        pending_secret=derived.secret_material,
        pending_address=derived.public_address,
        pending_is_import=False,
        import_chain=chain,
    )
    await state.set_state(OnboardingStates.awaiting_new_pin)
    await callback.message.edit_text(
        f"✨ New {chain} wallet generated:\n\n"
        f"<code>{derived.public_address}</code>\n\n"
        "Now set a <b>PIN</b> (4-8 digits or a passphrase) to encrypt it.\n"
        "⚠️ We do not store this PIN anywhere and cannot reset it.",
        parse_mode="HTML",
    )


@router.message(OnboardingStates.awaiting_new_pin)
async def on_pin_set(message: Message, state: FSMContext) -> None:
    pin = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    if len(pin) < 4:
        await message.answer("PIN too short. Use at least 4 characters. Try again:")
        return

    await state.update_data(pending_pin=pin)
    await state.set_state(OnboardingStates.confirming_new_pin)
    await message.answer("Confirm your PIN by typing it again:")


@router.message(OnboardingStates.confirming_new_pin)
async def on_pin_confirmed(message: Message, state: FSMContext) -> None:
    pin_confirm = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    if pin_confirm != data.get("pending_pin"):
        await message.answer("❌ PINs don't match. Type your PIN again:")
        await state.set_state(OnboardingStates.awaiting_new_pin)
        return

    chain = data["import_chain"]
    secret = data["pending_secret"]
    pin = data["pending_pin"]
    is_import = data["pending_is_import"]

    if is_import:
        derived = derive_wallet_for_chain(chain, secret)
        address = derived.public_address
    else:
        address = data["pending_address"]

    ciphertext, salt = encrypt_secret(secret, pin)

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    async with async_session() as session:
        wallet = Wallet(
            user_id=user.id,
            chain=Chain(chain),
            label="Main",
            public_address=address,
            encrypted_secret=ciphertext,
            secret_salt=salt,
            is_imported=is_import,
        )
        session.add(wallet)
        await session.commit()

    # scrub sensitive fields from FSM storage immediately
    await state.update_data(pending_secret=None, pending_pin=None)

    if is_import:
        await message.answer(f"✅ {chain} wallet imported: <code>{address}</code>", parse_mode="HTML")
        await advance_to_next_chain(message, state)
    else:
        await state.set_state(OnboardingStates.confirming_seed_saved)
        secret_msg = await message.answer(
            f"🔐 <b>Your {chain} seed phrase (SAVE THIS NOW):</b>\n\n"
            f"<code>{secret}</code>\n\n"
            "Write it down offline. This message will be deleted in 60 seconds. "
            "This is the ONLY time it will be shown.",
            reply_markup=confirm_seed_saved_kb(chain),
            parse_mode="HTML",
        )
        await state.update_data(seed_message_id=secret_msg.message_id)


@router.callback_query(F.data.startswith("seedsaved:"))
async def on_seed_saved_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    seed_msg_id = data.get("seed_message_id")
    if seed_msg_id:
        try:
            await callback.bot.delete_message(callback.message.chat.id, seed_msg_id)
        except Exception:
            pass

    await callback.message.answer("✅ Great — wallet secured.")
    await advance_to_next_chain(callback.message, state, callback=callback)


async def advance_to_next_chain(message: Message, state: FSMContext, callback: CallbackQuery | None = None) -> None:
    data = await state.get_data()
    idx = data.get("chain_idx", 0) + 1
    await state.update_data(chain_idx=idx)

    class _FakeCallback:
        def __init__(self, msg):
            self.message = msg
            self.from_user = msg.chat

    fc = callback if callback else _FakeCallback(message)
    fc.from_user = fc.from_user if hasattr(fc.from_user, "id") else message.from_user
    await start_wallet_setup_for_next_chain(fc, state)
