"""
solana_client.py
=================
Balance/holdings lookups, Jupiter quote+swap, and live tx monitoring for a
given address. Network calls only -- no key material handled here except a
Keypair passed in at call time for signing (never persisted by this module).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import settings

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class TokenHolding:
    mint: str
    symbol: str | None
    amount: float
    decimals: int
    usd_value: float | None = None
    price_usd: float | None = None


@dataclass
class QuoteResult:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    route_raw: dict


class SolanaClient:
    def __init__(self) -> None:
        self.rpc_url = settings.SOLANA_RPC_URL
        self.jupiter_url = settings.JUPITER_QUOTE_API
        if settings.HELIUS_API_KEY and "helius" not in self.rpc_url:
            logger.warning(
                "HELIUS_API_KEY is set but SOLANA_RPC_URL doesn't point at Helius. "
                "Set SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=%s for "
                "reliable production RPC (the public endpoint rate-limits heavily).",
                "***" if settings.HELIUS_API_KEY else "",
            )

    async def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with aiohttp.ClientSession() as session:
            async with session.post(self.rpc_url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"RPC error: {data['error']}")
                return data["result"]

    # ------------------------------------------------------------ reads -----

    async def get_sol_balance(self, address: str) -> float:
        result = await self._rpc("getBalance", [address])
        return result["value"] / LAMPORTS_PER_SOL

    async def get_token_holdings(self, address: str) -> list[TokenHolding]:
        """Fetch SPL token accounts for a wallet via getTokenAccountsByOwner."""
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"},
            ],
        )
        holdings = []
        for entry in result.get("value", []):
            info = entry["account"]["data"]["parsed"]["info"]
            amount = float(info["tokenAmount"]["uiAmountString"] or 0)
            if amount <= 0:
                continue
            holdings.append(
                TokenHolding(
                    mint=info["mint"],
                    symbol=None,  # enrich via token metadata / price service
                    amount=amount,
                    decimals=info["tokenAmount"]["decimals"],
                )
            )
        return holdings

    async def get_recent_signatures(self, address: str, limit: int = 20) -> list[dict]:
        return await self._rpc("getSignaturesForAddress", [address, {"limit": limit}])

    # ------------------------------------------------------------ swaps -----

    async def get_jupiter_quote(
        self, input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = 100
    ) -> QuoteResult:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(slippage_bps),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.jupiter_url}/quote", params=params) as resp:
                data = await resp.json()
        return QuoteResult(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            price_impact_pct=float(data.get("priceImpactPct", 0)),
            route_raw=data,
        )

    async def execute_jupiter_swap(self, quote: QuoteResult, keypair: Keypair) -> str:
        """
        Builds, signs, and submits a swap transaction from a Jupiter quote.
        `keypair` must be constructed by the caller from a decrypted secret
        held only transiently (see services/session_keys.py) -- this function
        does not persist or log it.
        """
        swap_payload = {
            "quoteResponse": quote.route_raw,
            "userPublicKey": str(keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.jupiter_url}/swap", json=swap_payload) as resp:
                swap_data = await resp.json()

        tx_bytes = bytes(swap_data["swapTransaction"], "utf-8")  # base64 in practice; decode properly
        import base64
        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        versioned_tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(versioned_tx.message, [keypair])

        result = await self._rpc(
            "sendTransaction",
            [base64.b64encode(bytes(signed_tx)).decode("utf-8"), {"encoding": "base64"}],
        )
        return result  # tx signature

    async def get_token_price_usd(self, mint: str) -> float | None:
        """Price via Jupiter price API (or swap to a stable as fallback)."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://price.jup.ag/v6/price?ids={mint}") as resp:
                data = await resp.json()
        entry = data.get("data", {}).get(mint)
        return float(entry["price"]) if entry else None


solana_client = SolanaClient()
