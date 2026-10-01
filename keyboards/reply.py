from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def persistent_menu_kb() -> ReplyKeyboardMarkup:
    """
    The 3 always-visible menu buttons the user requested, sitting below the
    chat input. Inline buttons (keyboards/inline.py) handle every actual
    action; these just jump to the right inline menu.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👛 My Wallet"),
                KeyboardButton(text="🔁 Copy Trade"),
                KeyboardButton(text="👀 Watchlist"),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
