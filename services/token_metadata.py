"""
token_metadata.py
==================
Resolves a raw mint/jetton address into a human-readable symbol/name, with a
short-lived in-memory cache so we're not hammering metadata APIs on every
render of the wallet dashboard or watchlist.

Solana: Jupiter's token list is the simplest free source for well-known
tokens; Helius DAS API (getAsset) covers long-tail/new tokens if you have a
Helius key configured.
TON: tonapi.io's jetton endpoint already returns symbol/name, so this is
mostly a caching wrapper for that one.
"""
from __future__ import annotations

import time

import aiohttp

from config import settings

_CACHE_TTL = 300  # 5 min
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


async def get_token_metadata(chain: str, address: str) -> dict:
    """Returns {"symbol": str|None, "name": str|None, "logo": str|None}."""
    cache_key = (chain, address)
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    if chain == "SOL":
        meta = await _fetch_solana_metadata(address)
    elif chain == "TON":
        meta = await _fetch_ton_metadata(address)
    else:
        meta = {"symbol": None, "name": None, "logo": None}

    _cache[cache_key] = (time.time(), meta)
    return meta


async def _fetch_solana_metadata(mint: str) -> dict:
    # Prefer Helius DAS API if configured -- covers new/long-tail tokens.
    if settings.HELIUS_API_KEY:
        try:
            url = f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
            payload = {
                "jsonrpc": "2.0", "id": "meta", "method": "getAsset",
                "params": {"id": mint},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
            content = data.get("result", {}).get("content", {})
            metadata = content.get("metadata", {})
            links = content.get("links", {})
            if metadata.get("symbol") or metadata.get("name"):
                return {
                    "symbol": metadata.get("symbol"),
                    "name": metadata.get("name"),
                    "logo": links.get("image"),
                }
        except Exception:
            pass  # fall through to Jupiter list

    # Fallback: Jupiter's static token list (covers most established tokens)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://tokens.jup.ag/token/{mint}", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "symbol": data.get("symbol"),
                        "name": data.get("name"),
                        "logo": data.get("logoURI"),
                    }
    except Exception:
        pass

    return {"symbol": None, "name": None, "logo": None}


async def _fetch_ton_metadata(jetton_address: str) -> dict:
    headers = {"X-API-Key": settings.TON_API_KEY} if settings.TON_API_KEY else {}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                f"{settings.TONAPI_BASE}/v2/jettons/{jetton_address}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    meta = data.get("metadata", {})
                    return {
                        "symbol": meta.get("symbol"),
                        "name": meta.get("name"),
                        "logo": meta.get("image"),
                    }
    except Exception:
        pass
    return {"symbol": None, "name": None, "logo": None}


def display_label(address: str, symbol: str | None) -> str:
    """Consistent 'SYMBOL' or fallback 'shortened address' for UI text."""
    return symbol if symbol else f"{address[:4]}...{address[-4:]}"
