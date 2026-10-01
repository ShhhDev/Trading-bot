"""
holdings.py
===========
Small shared helper so trading.py, copy_trade wiring, and anywhere else
that needs "how much of token X does wallet Y actually hold" doesn't
duplicate the chain-dispatch logic.
"""
from __future__ import annotations

from dataclasses import dataclass

from services.solana_client import solana_client
from services.ton_client import ton_client


@dataclass
class Holding:
    address: str          # mint (SOL) or jetton address (TON)
    symbol: str | None
    amount: float
    decimals: int


async def get_holdings(chain: str, public_address: str) -> list[Holding]:
    if chain == "SOL":
        raw = await solana_client.get_token_holdings(public_address)
        return [Holding(h.mint, h.symbol, h.amount, h.decimals) for h in raw]
    elif chain == "TON":
        raw = await ton_client.get_jetton_holdings(public_address)
        return [Holding(h.jetton_address, h.symbol, h.amount, h.decimals) for h in raw]
    raise ValueError(f"Unsupported chain: {chain}")


async def get_holding_amount(chain: str, public_address: str, token_address: str) -> float:
    """Returns 0.0 if the wallet holds none of this token (not an error)."""
    holdings = await get_holdings(chain, public_address)
    match = next((h for h in holdings if h.address == token_address), None)
    return match.amount if match else 0.0


async def get_price_usd(chain: str, token_address: str) -> float | None:
    if chain == "SOL":
        return await solana_client.get_token_price_usd(token_address)
    elif chain == "TON":
        return await ton_client.get_jetton_price_usd(token_address)
    return None
