"""
ton_client.py
=============
Balance/holdings and swap execution for TON via tonapi.io + STON.fi's swap
API. Structurally mirrors solana_client.py so the rest of the app (order
engine, handlers) can treat chains polymorphically.

⚠️ IMPORTANT -- READ BEFORE RELYING ON execute_swap() ⚠️
The swap-building logic below (get_swap_quote + execute_swap) is written
against STON.fi's documented v1 API shape as of this writing, but I do not
have live network access in this environment to actually call STON.fi's
endpoints and confirm response field names (ask_units, router_address,
swap_body, forward_amount, etc.) against their current live API. TON DEX
APIs have changed shape before (STON.fi went from v1 to a CPI-style router
contract; DeDust has a different response format entirely).

Before trusting this with real funds:
  1. Hit https://api.ston.fi/v1/swap/simulate manually (curl/Postman) with a
     real offer/ask pair and confirm the response fields match what's read
     here. Update field names if they've drifted.
  2. Test execute_swap() on TON testnet first (toncenter has a testnet
     endpoint) with a throwaway wallet before mainnet.
  3. Confirm tonsdk's create_transfer_message signature/payload handling
     still matches this tonsdk version pinned in requirements.txt --
     tonsdk has had breaking changes across versions.

This is a structurally complete implementation, not a verified-working one.
Treat it the way you'd treat any un-code-reviewed financial code path.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import aiohttp

from config import settings

STONFI_API_BASE = "https://api.ston.fi"
TON_NATIVE_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # STON.fi's placeholder for native TON
DEFAULT_FORWARD_TON = 250_000_000  # 0.25 TON reserved for gas/fees on the swap message


@dataclass
class JettonHolding:
    jetton_address: str
    symbol: str | None
    amount: float
    decimals: int
    usd_value: float | None = None
    price_usd: float | None = None


@dataclass
class TonQuoteResult:
    offer_address: str
    ask_address: str
    offer_amount: int          # raw units (respecting decimals) offered
    ask_amount: int            # raw units expected back
    min_ask_amount: int        # after slippage tolerance
    price_impact_pct: float
    router_address: str
    route_raw: dict


class TonClient:
    def __init__(self) -> None:
        self.api_base = settings.TON_API_BASE
        self.tonapi_base = settings.TONAPI_BASE
        self.stonfi_base = STONFI_API_BASE
        self.headers = {"X-API-Key": settings.TON_API_KEY} if settings.TON_API_KEY else {}

    async def get_ton_balance(self, address: str) -> float:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(f"{self.tonapi_base}/v2/accounts/{address}") as resp:
                data = await resp.json()
        return int(data.get("balance", 0)) / 1_000_000_000

    async def get_jetton_holdings(self, address: str) -> list[JettonHolding]:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(f"{self.tonapi_base}/v2/accounts/{address}/jettons") as resp:
                data = await resp.json()
        holdings = []
        for balance in data.get("balances", []):
            jetton = balance["jetton"]
            decimals = int(jetton.get("decimals", 9))
            raw_amount = int(balance["balance"])
            amount = raw_amount / (10**decimals)
            if amount <= 0:
                continue
            holdings.append(
                JettonHolding(
                    jetton_address=jetton["address"],
                    symbol=jetton.get("symbol"),
                    amount=amount,
                    decimals=decimals,
                )
            )
        return holdings

    async def get_recent_transactions(self, address: str, limit: int = 20) -> list[dict]:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            params = {"limit": limit}
            async with session.get(
                f"{self.tonapi_base}/v2/blockchain/accounts/{address}/transactions", params=params
            ) as resp:
                data = await resp.json()
        return data.get("transactions", [])

    # ------------------------------------------------------------ swaps -----

    async def get_swap_quote(
        self, offer_address: str, ask_address: str, amount: int, slippage_bps: int = 100
    ) -> TonQuoteResult:
        """
        offer_address/ask_address: jetton master addresses, or the special
        TON_NATIVE_ADDRESS constant for native TON.
        amount: raw units (already multiplied by decimals) being offered.
        """
        params = {
            "offer_address": offer_address,
            "ask_address": ask_address,
            "units": str(amount),
            "slippage_tolerance": str(slippage_bps / 10000),  # STON.fi expects a fraction, e.g. 0.01
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.stonfi_base}/v1/swap/simulate", params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"STON.fi quote failed ({resp.status}): {body[:200]}")
                data = await resp.json()

        ask_units = int(data["ask_units"])
        min_ask_units = int(data.get("min_ask_units", ask_units))
        price_impact = float(data.get("price_impact", 0)) * 100

        return TonQuoteResult(
            offer_address=offer_address,
            ask_address=ask_address,
            offer_amount=amount,
            ask_amount=ask_units,
            min_ask_amount=min_ask_units,
            price_impact_pct=price_impact,
            router_address=data["router_address"],
            route_raw=data,
        )

    async def execute_swap(self, quote: TonQuoteResult, mnemonic: str) -> str:
        """
        Builds, signs, and broadcasts a STON.fi swap from the user's wallet.

        `mnemonic` is sourced transiently from services/session_keys.py by
        the caller (services/order_engine.py) and is never logged or
        persisted by this function.

        Returns the transaction hash (BOC hash) once accepted by the network.
        Note: unlike Solana's sendTransaction, toncenter's sendBoc returning
        successfully means the message was ACCEPTED for processing, not that
        it has been confirmed included in a block yet -- callers that need
        fill confirmation should poll get_recent_transactions afterward.
        """
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import to_nano

        words = mnemonic.strip().split()
        _mnemonic, _pub, _priv, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)

        route = quote.route_raw
        # STON.fi's simulate response includes everything needed to build the
        # transfer message: the jetton wallet to send FROM (the user's own
        # jetton wallet for the offer token, or router address for native TON
        # swaps), the forward payload (BOC) encoding the swap instruction for
        # the router, and how much TON to attach for gas.
        to_address = route.get("router_address") if quote.offer_address == TON_NATIVE_ADDRESS else route.get("offer_jetton_wallet")
        forward_payload_b64 = route.get("swap_body")  # base64 BOC payload from STON.fi
        forward_ton_amount = int(route.get("forward_amount", DEFAULT_FORWARD_TON))

        if not to_address or not forward_payload_b64:
            raise RuntimeError(
                "STON.fi quote response missing router/payload fields -- "
                "STON.fi's API shape may have changed; check route_raw for the current field names."
            )

        from tonsdk.boc import Cell
        payload_cell = Cell.one_from_boc(base64.b64decode(forward_payload_b64))

        query = wallet.create_transfer_message(
            to_addr=to_address,
            amount=to_nano(forward_ton_amount / 1_000_000_000, "ton"),
            seqno=await self._get_seqno(wallet.address.to_string(True, True, True)),
            payload=payload_cell,
        )
        boc_b64 = base64.b64encode(query["message"].to_boc(False)).decode("utf-8")

        return await self._send_boc(boc_b64)

    async def _get_seqno(self, address: str) -> int:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(
                f"{self.tonapi_base}/v2/wallet/{address}/seqno",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return 0  # brand-new wallet, not yet deployed on-chain
                data = await resp.json()
                return int(data.get("seqno", 0))

    async def _send_boc(self, boc_b64: str) -> str:
        headers = {"Content-Type": "application/json", **self.headers}
        payload = {"boc": boc_b64}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{self.api_base}/sendBoc", json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        if not data.get("ok", False):
            raise RuntimeError(f"TON broadcast failed: {data}")
        return data.get("result", {}).get("hash", "")

    async def get_jetton_price_usd(self, jetton_address: str) -> float | None:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(f"{self.tonapi_base}/v2/rates?tokens={jetton_address}&currencies=usd") as resp:
                data = await resp.json()
        rate = data.get("rates", {}).get(jetton_address, {}).get("prices", {}).get("USD")
        return float(rate) if rate else None

    async def get_ton_price_usd(self) -> float | None:
        """Native TON/USD price -- needed for USD-based sizing (guardrails, copy-trade)."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(f"{self.tonapi_base}/v2/rates?tokens=ton&currencies=usd") as resp:
                data = await resp.json()
        rate = data.get("rates", {}).get("ton", {}).get("prices", {}).get("USD")
        return float(rate) if rate else None


ton_client = TonClient()
