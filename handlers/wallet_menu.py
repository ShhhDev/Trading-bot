from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from keyboards.inline import (
    chain_switch_kb, main_menu_kb, token_action_kb, wallet_menu_kb, slippage_kb,
)
from models.db import User, Wallet, async_session
from services.solana_client import solana_client
from services.token_metadata import display_label, get_token_metadata
from services.ton_client import ton_client

router = Router(name="wallet_menu")


async def _get_user_and_wallets(telegram_id: int) -> tuple[User | None, list[Wallet]]:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return None, []
        result = await session.execute(select(Wallet).where(Wallet.user_id == user.id, Wallet.is_active == True))  # noqa: E712
        wallets = list(result.scalars().all())
        return user, wallets


@router.message(F.text == "👛 My Wallet")
@router.callback_query(F.data == "menu:wallet")
async def open_wallet_menu(event: Message | CallbackQuery, state: FSMContext) -> None:
    telegram_id = event.from_user.id
    user, wallets = await _get_user_and_wallets(telegram_id)

    target = event.message if isinstance(event, CallbackQuery) else event

    if not wallets:
        await target.answer("You don't have any wallets yet. Use /start to set one up.")
        return

    chains = sorted({w.chain.value for w in wallets})
    if len(chains) == 1:
        await render_wallet_dashboard(target, telegram_id, chains[0])
    else:
        await target.answer("Which wallet?", reply_markup=chain_switch_kb(chains))


@router.callback_query(F.data.startswith("menu:wallet:"))
async def open_specific_wallet(callback: CallbackQuery, state: FSMContext) -> None:
    chain = callback.data.split(":")[2]
    await render_wallet_dashboard(callback.message, callback.from_user.id, chain, edit=True)


async def render_wallet_dashboard(target: Message, telegram_id: int, chain: str, edit: bool = False) -> None:
    user, wallets = await _get_user_and_wallets(telegram_id)
    wallet = next((w for w in wallets if w.chain.value == chain), None)
    if not wallet:
        await target.answer(f"No {chain} wallet found.")
        return

    if chain == "SOL":
        balance = await solana_client.get_sol_balance(wallet.public_address)
        holdings = await solana_client.get_token_holdings(wallet.public_address)
        balance_line = f"◎ {balance:.4f} SOL"
    else:
        balance = await ton_client.get_ton_balance(wallet.public_address)
        holdings = await ton_client.get_jetton_holdings(wallet.public_address)
        balance_line = f"💎 {balance:.4f} TON"

    text = (
        f"<b>{chain} Wallet</b>\n"
        f"<code>{wallet.public_address}</code>\n\n"
        f"Balance: {balance_line}\n"
        f"Holdings: {len(holdings)} token(s)\n\n"
        f"Slippage: {wallet.user.default_slippage_bps / 100 if hasattr(wallet, 'user') else '1.0'}%"
    )

    kb = wallet_menu_kb(chain)
    if edit:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("wallet:holdings:"))
async def show_holdings(callback: CallbackQuery) -> None:
    chain = callback.data.split(":")[2]
    _, wallets = await _get_user_and_wallets(callback.from_user.id)
    wallet = next((w for w in wallets if w.chain.value == chain), None)
    if not wallet:
        await callback.answer("Wallet not found", show_alert=True)
        return

    if chain == "SOL":
        holdings = await solana_client.get_token_holdings(wallet.public_address)
    else:
        holdings = await ton_client.get_jetton_holdings(wallet.public_address)

    if not holdings:
        await callback.message.answer("No token holdings found in this wallet yet.")
        return

    for h in holdings[:15]:  # cap to avoid flooding chat
        addr = h.mint if chain == "SOL" else h.jetton_address
        meta = await get_token_metadata(chain, addr)
        symbol = display_label(addr, meta["symbol"])
        text = f"<b>{symbol}</b>\nAmount: {h.amount:,.4f}\nCA: <code>{addr}</code>"
        await callback.message.answer(text, reply_markup=token_action_kb(chain, addr), parse_mode="HTML")


@router.callback_query(F.data.startswith("wallet:deposit:"))
async def show_deposit(callback: CallbackQuery) -> None:
    chain = callback.data.split(":")[2]
    _, wallets = await _get_user_and_wallets(callback.from_user.id)
    wallet = next((w for w in wallets if w.chain.value == chain), None)
    if not wallet:
        await callback.answer("Wallet not found", show_alert=True)
        return
    await callback.message.answer(
        f"📥 <b>Deposit {chain}</b>\n\nSend {chain} (or SPL/Jetton tokens) to:\n\n"
        f"<code>{wallet.public_address}</code>\n\n"
        "I'll notify you as soon as a deposit lands.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("wallet:slippage:"))
async def show_slippage(callback: CallbackQuery) -> None:
    chain = callback.data.split(":")[2]
    await callback.message.edit_text(
        "Choose default slippage tolerance:", reply_markup=slippage_kb(chain)
    )


@router.callback_query(F.data.startswith("slippage:set:"))
async def set_slippage(callback: CallbackQuery) -> None:
    _, _, chain, pct = callback.data.split(":")
    bps = int(float(pct) * 100)
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.default_slippage_bps = bps
            await session.commit()
    await callback.answer(f"Slippage set to {pct}%")
    await render_wallet_dashboard(callback.message, callback.from_user.id, chain, edit=True)


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Main menu:", reply_markup=main_menu_kb())
