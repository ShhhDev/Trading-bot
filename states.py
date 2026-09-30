from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    choosing_chain = State()
    choosing_wallet_action = State()   # import vs create, per chain
    awaiting_seed_import = State()
    awaiting_new_pin = State()
    confirming_new_pin = State()
    awaiting_unlock_pin = State()
    confirming_seed_saved = State()


class TradeStates(StatesGroup):
    awaiting_ca_for_buy = State()
    awaiting_ca_for_sell = State()
    awaiting_buy_amount = State()
    awaiting_sell_pct = State()
    awaiting_limit_price = State()
    awaiting_limit_amount = State()
    awaiting_tp_pct = State()
    awaiting_sl_pct = State()
    awaiting_tp_sl_sell_pct = State()
    awaiting_slippage = State()
    awaiting_pin_to_confirm = State()
    awaiting_guardrail_value = State()


class SwapStates(StatesGroup):
    awaiting_from_ca = State()
    awaiting_to_ca = State()
    awaiting_swap_amount = State()


class WatchlistStates(StatesGroup):
    awaiting_ca = State()
    awaiting_alert_pct = State()


class CopyTradeStates(StatesGroup):
    awaiting_trader_address = State()
    awaiting_label = State()
    choosing_mode = State()
    awaiting_min_buy_usd = State()
    awaiting_copy_amount = State()
    awaiting_slippage = State()
