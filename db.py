from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer,
    LargeBinary, String, UniqueConstraint, func
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Chain(str, enum.Enum):
    SOL = "SOL"
    TON = "TON"

class OrderType(str, enum.Enum):
    MARKET_BUY = "MARKET_BUY"
    MARKET_SELL = "MARKET_SELL"
    LIMIT_BUY = "LIMIT_BUY"
    LIMIT_SELL = "LIMIT_SELL"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"       # waiting for trigger condition
    SUBMITTED = "SUBMITTED"   # sent to chain, awaiting confirmation
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class CopyMode(str, enum.Enum):
    NOTIFY_ONLY = "NOTIFY_ONLY"
    AUTO_COPY = "AUTO_COPY"


# ---------------------------------------------------------------- User -----

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_chains: Mapped[str] = mapped_column(String(16), default="")  # "SOL", "TON", "SOL,TON"
    default_slippage_bps: Mapped[int] = mapped_column(Integer, default=100)  # 1.00%
    pin_hash_check: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pin_salt_check: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Guardrails -- soft limits the user (or an admin) sets to protect against
    # fat-finger errors and runaway auto-copy activity. None = no limit.
    max_trade_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_daily_volume_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    require_confirm_above_usd: Mapped[float | None] = mapped_column(Float, default=500.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    wallets: Mapped[list["Wallet"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    watchlist: Mapped[list["WatchlistItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    copy_configs: Mapped[list["CopyTradeConfig"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# -------------------------------------------------------------- Wallet -----

class Wallet(Base):
    """
    Stores only: public address + encrypted secret + salt.
    Plaintext private key / seed NEVER stored. See services/crypto_vault.py.
    """
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    chain: Mapped[Chain] = mapped_column(Enum(Chain))
    label: Mapped[str] = mapped_column(String(64), default="Main")
    public_address: Mapped[str] = mapped_column(String(128), index=True)

    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary)  # Fernet ciphertext
    secret_salt: Mapped[bytes] = mapped_column(LargeBinary)       # scrypt salt
    is_imported: Mapped[bool] = mapped_column(Boolean, default=False)  # vs bot-generated

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="wallets")
    orders: Mapped[list["Order"]] = relationship(back_populates="wallet")

    __table_args__ = (UniqueConstraint("user_id", "chain", "label", name="uq_user_chain_label"),)


# --------------------------------------------------------------- Order -----

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    chain: Mapped[Chain] = mapped_column(Enum(Chain))

    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)

    token_address: Mapped[str] = mapped_column(String(128), index=True)  # CA being traded
    token_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # For market orders: amount_in is fixed. For limit/TP/SL: trigger_price gates execution.
    amount_in: Mapped[float] = mapped_column(Float)  # in SOL/TON or in token units, see is_amount_in_base
    is_amount_in_base: Mapped[bool] = mapped_column(Boolean, default=True)  # True=SOL/TON, False=token qty
    trigger_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_bps: Mapped[int] = mapped_column(Integer, default=100)

    # For TP/SL expressed as % move from entry rather than absolute price
    trigger_pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    tx_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="orders")
    wallet: Mapped["Wallet"] = relationship(back_populates="orders")


# ---------------------------------------------------------- Watchlist -----

class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    chain: Mapped[Chain] = mapped_column(Enum(Chain))
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)

    alert_on_pct_move: Mapped[float | None] = mapped_column(Float, nullable=True)  # e.g. 5.0 = alert every +/-5%
    reference_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_alert_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="watchlist")

    __table_args__ = (UniqueConstraint("user_id", "chain", "token_address", name="uq_user_chain_token"),)


# ------------------------------------------------------------ CopyTrade -----

class CopyTradeConfig(Base):
    __tablename__ = "copy_trade_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    chain: Mapped[Chain] = mapped_column(Enum(Chain))
    trader_address: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mode: Mapped[CopyMode] = mapped_column(Enum(CopyMode), default=CopyMode.NOTIFY_ONLY)
    min_trader_buy_usd: Mapped[float] = mapped_column(Float, default=0.0)   # ignore trades below this
    copy_amount_usd: Mapped[float] = mapped_column(Float, default=0.0)     # fixed $ to copy with
    copy_amount_is_pct_of_trader: Mapped[bool] = mapped_column(Boolean, default=False)  # alt: mirror % size
    max_slippage_bps: Mapped[int] = mapped_column(Integer, default=300)
    also_copy_sells: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="copy_configs")

    __table_args__ = (UniqueConstraint("user_id", "chain", "trader_address", name="uq_user_chain_trader"),)


# ------------------------------------------------------------- TradeLog -----

class TradeLog(Base):
    """
    Records every executed trade's USD value at fill time. Used to enforce
    max_daily_volume_usd guardrails (see services/guardrails.py) and doubles
    as a basic trade history the user could eventually view.
    """
    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain))
    token_address: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8))  # "BUY" | "SELL"
    usd_value: Mapped[float] = mapped_column(Float)
    tx_signature: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | copy_trade | limit_order | tp_sl
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


# --------------------------------------------------------------- Engine -----

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
