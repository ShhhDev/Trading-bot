from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from models.db import Chain, User, Wallet, async_session
from services.guardrails import GuardrailViolation
from services.order_engine import InsufficientUnlockError, execute_market_order
from services.wallet_service import is_valid_address
from states import SwapStates

router = Router(name="swap")

NATIVE_LABEL = {"SOL": "SOL (native)", "TON": "TON (native)"}


@router.callback_query(F.data.startswith("swap:start:"))
async def start_swap(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[2]
    await state.update_data(swap_chain=chain)
    await state.set_state(SwapStates.awaiting_from_ca)
    await callback.message.edit_text(
        f"🔄 <b>Swap on {chain}</b>\n\n"
        f"Send the CA of the token you're swapping <b>FROM</b> "
        f"(or type <code>native</code> for {NATIVE_LABEL[chain]}):",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("swap:from:"))
async def start_swap_from_token(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, chain, token = callback.data.split(":")
    await state.update_data(swap_chain=chain, swap_from=token)
    await state.set_state(SwapStates.awaiting_to_ca)
    await callback.message.edit_text("Send the CA of the token you're swapping <b>TO</b>:", parse_mode="HTML")


@router.message(SwapStates.awaiting_from_ca)
async def on_swap_from(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    chain = data["swap_chain"]
    from_addr = "native" if text.lower() == "native" else text

    if from_addr != "native" and not is_valid_address(chain, from_addr):
        await message.answer(f"❌ Not a valid {chain} address. Try again, or type `native`:", parse_mode="Markdown")
        return

    await state.update_data(swap_from=from_addr)
    await state.set_state(SwapStates.awaiting_to_ca)
    await message.answer("Now send the CA you're swapping <b>TO</b>:", parse_mode="HTML")


@router.message(SwapStates.awaiting_to_ca)
async def on_swap_to(message: Message, state: FSMContext) -> None:
    to_addr = message.text.strip()
    data = await state.get_data()
    chain = data["swap_chain"]

    if not is_valid_address(chain, to_addr):
        await message.answer(f"❌ Not a valid {chain} address. Try again:")
        return
    if to_addr == data.get("swap_from"):
        await message.answer("You can't swap a token into itself. Send a different CA:")
        return

    await state.update_data(swap_to=to_addr)
    await state.set_state(SwapStates.awaiting_swap_amount)
    await message.answer("Enter amount to swap:")


@router.message(SwapStates.awaiting_swap_amount)
async def on_swap_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.strip())
    except ValueError:
        await message.answer("Please send a valid number.")
        return

    data = await state.get_data()
    chain = data["swap_chain"]
    from_addr = data["swap_from"]
    to_addr = data["swap_to"]
    telegram_id = message.from_user.id

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
        result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.chain == Chain(chain))
        )
        wallet = result.scalar_one_or_none()

    if not wallet:
        await message.answer("Wallet not found.")
        return

    status = await message.answer(f"⏳ Swapping {amount} {from_addr[:8] if from_addr != 'native' else chain} → {to_addr[:8]}...")

    try:
        # If from == native, this is functionally a "buy" of `to_addr`.
        # If to == native, it's functionally a "sell" of `from_addr`.
        # Arbitrary token-to-token swaps route through native under the hood
        # on both Jupiter and STON.fi/DeDust anyway.
        if from_addr == "native":
            tx_sig = await execute_market_order(
                user.id, wallet, to_addr, "BUY", amount, user.default_slippage_bps
            )
        else:
            tx_sig = await execute_market_order(
                user.id, wallet, from_addr, "SELL", amount, user.default_slippage_bps
            )
        await status.edit_text(f"✅ Swap complete!\nTx: <code>{tx_sig}</code>", parse_mode="HTML")
    except InsufficientUnlockError:
        await status.edit_text("🔒 Wallet locked. Unlock with your PIN first, then retry.")
    except GuardrailViolation as e:
        await status.edit_text(f"🛑 {e}")
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ Swap failed: {e}")

    await state.clear()
