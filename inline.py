from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


# --------------------------------------------------------- Onboarding -----

def chain_selection_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="◎ Solana", callback_data="chain:SOL")
    b.button(text="💎 TON", callback_data="chain:TON")
    b.button(text="◎ + 💎 Both", callback_data="chain:BOTH")
    b.adjust(2, 1)
    return b.as_markup()


def wallet_action_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔑 I have a wallet (Import)", callback_data=f"walletaction:import:{chain}")
    b.button(text="✨ Create new wallet", callback_data=f"walletaction:create:{chain}")
    b.adjust(1)
    return b.as_markup()


def confirm_seed_saved_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ I've saved it safely", callback_data=f"seedsaved:{chain}")
    b.adjust(1)
    return b.as_markup()


def import_warning_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚠️ I understand the risks, continue", callback_data=f"importconfirm:{chain}")
    b.button(text="◀️ Cancel", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


# ------------------------------------------------------------- Menus -----

def main_menu_kb() -> InlineKeyboardMarkup:
    """Sits above the 3 persistent reply-keyboard buttons; used for /start."""
    b = InlineKeyboardBuilder()
    b.button(text="👛 My Wallet", callback_data="menu:wallet")
    b.button(text="🔁 Copy Trade", callback_data="menu:copytrade")
    b.button(text="👀 Watchlist", callback_data="menu:watchlist")
    b.adjust(1)
    return b.as_markup()


def wallet_menu_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 Buy", callback_data=f"trade:buy:{chain}")
    b.button(text="📤 Sell", callback_data=f"trade:sell:{chain}")
    b.button(text="🔄 Swap", callback_data=f"swap:start:{chain}")
    b.button(text="📊 Holdings", callback_data=f"wallet:holdings:{chain}")
    b.button(text="📥 Deposit", callback_data=f"wallet:deposit:{chain}")
    b.button(text="⚙️ Orders (Limit/TP/SL)", callback_data=f"orders:list:{chain}")
    b.button(text="🎚 Slippage", callback_data=f"wallet:slippage:{chain}")
    b.button(text="🛡 Trade Limits", callback_data="wallet:guardrails")
    b.button(text="🔓 Unlock wallet (PIN)", callback_data=f"wallet:unlock:{chain}")
    b.button(text="◀️ Back", callback_data="menu:main")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


def chain_switch_kb(active: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in active:
        b.button(text=f"{'◎' if c == 'SOL' else '💎'} {c} Wallet", callback_data=f"menu:wallet:{c}")
    b.button(text="◀️ Back", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


# --------------------------------------------------------- Trade flow -----

def buy_amount_presets_kb(chain: str) -> InlineKeyboardMarkup:
    presets = {"SOL": ["0.1", "0.5", "1", "5", "10", "Custom"], "TON": ["1", "5", "10", "50", "100", "Custom"]}
    b = InlineKeyboardBuilder()
    for amt in presets.get(chain, presets["SOL"]):
        cb = "trade:customamount" if amt == "Custom" else f"trade:amt:{amt}"
        b.button(text=f"{amt} {chain}" if amt != "Custom" else "✏️ Custom amount", callback_data=cb)
    b.adjust(3, 3)
    return b.as_markup()


def sell_pct_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for pct in [25, 50, 75, 100]:
        b.button(text=f"{pct}%", callback_data=f"trade:sellpct:{pct}")
    b.button(text="✏️ Custom %", callback_data="trade:sellpct:custom")
    b.adjust(4, 1)
    return b.as_markup()


def post_buy_kb(token_address: str, chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📤 Sell", callback_data=f"trade:sell:{chain}:{token_address}")
    b.button(text="🎯 Set Take Profit", callback_data=f"order:tp:{chain}:{token_address}")
    b.button(text="🛑 Set Stop Loss", callback_data=f"order:sl:{chain}:{token_address}")
    b.button(text="👀 Add to Watchlist", callback_data=f"watch:add:{chain}:{token_address}")
    b.button(text="◀️ Back to Wallet", callback_data=f"menu:wallet:{chain}")
    b.adjust(2, 2, 1)
    return b.as_markup()


def token_action_kb(chain: str, token_address: str) -> InlineKeyboardMarkup:
    """Shown against a holding -- CA-specific action row."""
    b = InlineKeyboardBuilder()
    b.button(text="💰 Buy More", callback_data=f"trade:buy:{chain}:{token_address}")
    b.button(text="📤 Sell", callback_data=f"trade:sell:{chain}:{token_address}")
    b.button(text="🔄 Swap", callback_data=f"swap:from:{chain}:{token_address}")
    b.button(text="🎯 TP", callback_data=f"order:tp:{chain}:{token_address}")
    b.button(text="🛑 SL", callback_data=f"order:sl:{chain}:{token_address}")
    b.button(text="👀 Watch", callback_data=f"watch:add:{chain}:{token_address}")
    b.adjust(2, 2, 2)
    return b.as_markup()


def order_type_kb(chain: str, token_address: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Market Buy", callback_data=f"order:marketbuy:{chain}:{token_address}")
    b.button(text="📉 Limit Buy", callback_data=f"order:limitbuy:{chain}:{token_address}")
    b.button(text="📈 Limit Sell", callback_data=f"order:limitsell:{chain}:{token_address}")
    b.button(text="🎯 Take Profit", callback_data=f"order:tp:{chain}:{token_address}")
    b.button(text="🛑 Stop Loss", callback_data=f"order:sl:{chain}:{token_address}")
    b.adjust(1, 2, 2)
    return b.as_markup()


def slippage_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for pct in [0.5, 1, 3, 5, 10]:
        b.button(text=f"{pct}%", callback_data=f"slippage:set:{chain}:{pct}")
    b.button(text="✏️ Custom", callback_data=f"slippage:custom:{chain}")
    b.adjust(3, 2, 1)
    return b.as_markup()


def guardrails_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Set max per-trade $", callback_data="guardrail:set:max_trade")
    b.button(text="Set max daily volume $", callback_data="guardrail:set:max_daily")
    b.button(text="Set confirm-above $", callback_data="guardrail:set:confirm_above")
    b.button(text="🗑 Clear all limits", callback_data="guardrail:clear")
    b.button(text="◀️ Back", callback_data="menu:wallet")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------- Watchlist -----

def watchlist_item_kb(chain: str, token_address: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Set Alert %", callback_data=f"watch:setalert:{chain}:{token_address}")
    b.button(text="💰 Buy", callback_data=f"trade:buy:{chain}:{token_address}")
    b.button(text="🗑 Remove", callback_data=f"watch:remove:{chain}:{token_address}")
    b.adjust(1, 2)
    return b.as_markup()


def watchlist_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add token to watchlist", callback_data="watch:addnew")
    b.button(text="◀️ Back", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


# --------------------------------------------------------- Copy trade -----

def copytrade_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add trader wallet", callback_data="copytrade:add")
    b.button(text="📋 My copy configs", callback_data="copytrade:list")
    b.button(text="◀️ Back", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def copytrade_mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Notify me only", callback_data="copytrade:mode:notify")
    b.button(text="🔁 Auto-copy trades", callback_data="copytrade:mode:auto")
    b.adjust(1)
    return b.as_markup()


def copytrade_item_kb(config_id: int, is_active: bool, mode: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    toggle_text = "⏸ Pause" if is_active else "▶️ Resume"
    b.button(text=toggle_text, callback_data=f"copytrade:toggle:{config_id}")
    mode_text = "🔁 Switch to Auto-copy" if mode == "NOTIFY_ONLY" else "🔔 Switch to Notify-only"
    b.button(text=mode_text, callback_data=f"copytrade:switchmode:{config_id}")
    b.button(text="⚙️ Edit settings", callback_data=f"copytrade:edit:{config_id}")
    b.button(text="🗑 Remove", callback_data=f"copytrade:remove:{config_id}")
    b.adjust(1, 1, 2)
    return b.as_markup()


def copytrade_amount_mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💵 Fixed $ amount", callback_data="copytrade:amtmode:fixed")
    b.button(text="📊 % of trader's size", callback_data="copytrade:amtmode:pct")
    b.adjust(1)
    return b.as_markup()


# -------------------------------------------------------------- Misc -----

def confirm_cancel_kb(confirm_cb: str, cancel_cb: str = "menu:main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm", callback_data=confirm_cb)
    b.button(text="❌ Cancel", callback_data=cancel_cb)
    b.adjust(2)
    return b.as_markup()


def confirm_large_buy_kb(chain: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm buy", callback_data="trade:confirmbuy")
    b.button(text="❌ Cancel", callback_data=f"menu:wallet:{chain}")
    b.adjust(2)
    return b.as_markup()


def back_kb(target: str = "menu:main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="◀️ Back", callback_data=target)
    return b.as_markup()
