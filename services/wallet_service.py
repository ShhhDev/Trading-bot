"""
wallet_service.py
==================
Mnemonic generation/validation and keypair derivation for Solana and TON.
No network calls here -- pure crypto/derivation. Network (balances, sends)
lives in services/solana_client.py and services/ton_client.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import base58
from bip_utils import (
    Bip39MnemonicGenerator, Bip39MnemonicValidator, Bip39SeedGenerator,
    Bip39WordsNum, Bip44, Bip44Coins,
)
from solders.keypair import Keypair as SolKeypair


@dataclass
class DerivedWallet:
    public_address: str
    secret_material: str  # what we encrypt: mnemonic (preferred) or raw key


# ------------------------------------------------------------- Mnemonic -----

def generate_mnemonic(words: int = 24) -> str:
    words_num = Bip39WordsNum.WORDS_NUM_24 if words == 24 else Bip39WordsNum.WORDS_NUM_12
    return str(Bip39MnemonicGenerator().FromWordsNumber(words_num))


def is_valid_mnemonic(phrase: str) -> bool:
    phrase = " ".join(phrase.strip().split())  # normalize whitespace
    return Bip39MnemonicValidator().IsValid(phrase)


# --------------------------------------------------------------- Solana -----

SOL_DERIVATION_PATH = "m/44'/501'/0'/0'"  # standard Phantom/Solflare path


def derive_solana_wallet_from_mnemonic(mnemonic: str) -> DerivedWallet:
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).DeriveDefaultPath()
    priv_key_bytes = bip44_ctx.PrivateKey().Raw().ToBytes()
    keypair = SolKeypair.from_seed(priv_key_bytes)
    return DerivedWallet(public_address=str(keypair.pubkey()), secret_material=mnemonic)


def solana_keypair_from_mnemonic(mnemonic: str) -> SolKeypair:
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).DeriveDefaultPath()
    priv_key_bytes = bip44_ctx.PrivateKey().Raw().ToBytes()
    return SolKeypair.from_seed(priv_key_bytes)


def solana_keypair_from_raw_private_key(base58_key: str) -> SolKeypair:
    """Support users who paste a raw base58 private key instead of a mnemonic."""
    return SolKeypair.from_bytes(base58.b58decode(base58_key))


def is_valid_solana_private_key(base58_key: str) -> bool:
    try:
        SolKeypair.from_bytes(base58.b58decode(base58_key))
        return True
    except Exception:
        return False


def new_solana_wallet() -> DerivedWallet:
    mnemonic = generate_mnemonic(24)
    return derive_solana_wallet_from_mnemonic(mnemonic)


# ------------------------------------------------------------------ TON -----
# TON commonly uses its own 24-word mnemonic scheme (TON-specific wordlist
# behavior differs slightly from BIP39 but tonsdk handles this). We keep it
# isolated here so the rest of the app doesn't care about the difference.

def new_ton_wallet() -> DerivedWallet:
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    from tonsdk.crypto import mnemonic_new

    mnemonic_words = mnemonic_new()
    mnemonic = " ".join(mnemonic_words)
    _mnemonic, _pub, _priv, wallet = Wallets.from_mnemonics(
        mnemonic_words, WalletVersionEnum.v4r2, 0
    )
    address = wallet.address.to_string(True, True, True)
    return DerivedWallet(public_address=address, secret_material=mnemonic)


def is_valid_ton_mnemonic(phrase: str) -> bool:
    from tonsdk.crypto import mnemonic_is_valid
    words = phrase.strip().split()
    if len(words) not in (12, 24):
        return False
    try:
        return mnemonic_is_valid(words)
    except Exception:
        return False


def derive_ton_wallet_from_mnemonic(mnemonic: str) -> DerivedWallet:
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum

    words = mnemonic.strip().split()
    _mnemonic, _pub, _priv, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
    address = wallet.address.to_string(True, True, True)
    return DerivedWallet(public_address=address, secret_material=mnemonic)


# --------------------------------------------------------------- Dispatch -----

def validate_secret_for_chain(chain: str, secret: str) -> bool:
    secret = secret.strip()
    if chain == "SOL":
        word_count = len(secret.split())
        if word_count in (12, 24):
            return is_valid_mnemonic(secret)
        return is_valid_solana_private_key(secret)
    elif chain == "TON":
        return is_valid_ton_mnemonic(secret)
    return False


def derive_wallet_for_chain(chain: str, secret: str) -> DerivedWallet:
    secret = secret.strip()
    if chain == "SOL":
        word_count = len(secret.split())
        if word_count in (12, 24):
            return derive_solana_wallet_from_mnemonic(secret)
        keypair = solana_keypair_from_raw_private_key(secret)
        return DerivedWallet(public_address=str(keypair.pubkey()), secret_material=secret)
    elif chain == "TON":
        return derive_ton_wallet_from_mnemonic(secret)
    raise ValueError(f"Unsupported chain: {chain}")


def generate_wallet_for_chain(chain: str) -> DerivedWallet:
    if chain == "SOL":
        return new_solana_wallet()
    elif chain == "TON":
        return new_ton_wallet()
    raise ValueError(f"Unsupported chain: {chain}")


# ------------------------------------------------------- Address validation -----
# For validating a pasted CA (contract/token address) or trader wallet address --
# distinct from validate_secret_for_chain, which validates seeds/private keys.

def is_valid_address(chain: str, address: str) -> bool:
    """
    Structural validation only (correct format/checksum). Does NOT confirm
    the address is a real, funded, or tradeable token/wallet on-chain --
    callers should still handle "not found" / "no liquidity" errors from
    the RPC or swap API gracefully.
    """
    address = address.strip()
    if not address:
        return False

    if chain == "SOL":
        try:
            from solders.pubkey import Pubkey
            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    elif chain == "TON":
        # TON addresses come in a few forms (raw "0:hex", or base64/base64url
        # user-friendly). tonsdk's Address handles all of them.
        try:
            from tonsdk.utils import Address
            Address(address)
            return True
        except Exception:
            return False

    return False
