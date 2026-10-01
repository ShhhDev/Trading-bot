"""
crypto_vault.py
================
The single most security-critical module in this codebase. Read this whole
docstring before touching anything below.

THREAT MODEL
------------
This bot is necessarily custodial-ish: to auto-execute trades it needs to be
able to sign transactions without the user typing their PIN on every single
click. That is a real risk. This module's job is to make a full database
leak, by itself, NOT enough to steal funds.

DESIGN
------
- Every user picks a PIN (or passphrase) at wallet-creation/import time.
- The PIN is NEVER stored anywhere, in any form, ever.
- We derive a per-user encryption key with scrypt from:
      KDF(pin + MASTER_PEPPER, salt=user_specific_random_salt)
- The private key / seed phrase is encrypted with that derived key (Fernet /
  AES-GCM) and ONLY the ciphertext + salt are stored in the DB.
- To sign a trade, the bot needs the PIN. Two supported modes (see
  services/session_keys.py):
    1. "Session unlock": user enters PIN once, we hold the DECRYPTED key
       in memory only (never on disk) for a short TTL (e.g. 15 min) tied to
       their Telegram user id, then wipe it.
    2. "Confirm every trade": user re-enters PIN (or a lighter confirm step)
       per trade. More friction, more safety. Should be the default for
       large/imported wallets.
- Bot-generated "trading hot wallets" (the recommended path) can optionally
  use a lower-friction mode SINCE the user is explicitly told to only keep
  trading-sized funds there -- never their main holdings.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------
- It does not log plaintext seeds/keys under any log level, ever.
- It does not send plaintext seeds/keys back to Telegram after initial
  display (Telegram message = shown once, then the bot's own copy of that
  outbound message is deleted from chat history where possible).
- It does not support "forgot PIN" recovery. If a user loses their PIN AND
  doesn't have their own seed phrase backup, funds in a bot-managed wallet
  are unrecoverable by design. This must be disclosed to users up front.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from config import settings

SALT_BYTES = 16
SCRYPT_N = 2**15  # ~32ms-ish on modern hardware, tune per your infra
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32


class DecryptionError(Exception):
    """Raised when a PIN is wrong or ciphertext is corrupted/tampered."""


def generate_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def _derive_key(pin: str, salt: bytes) -> bytes:
    """
    Derive a 32-byte key from user PIN + server-side pepper + per-user salt.
    Pepper means a raw DB leak (ciphertext + salt, no pepper) is insufficient;
    pepper theft alone (no DB) is also insufficient. Both must be compromised
    AND the PIN must be brute-forced (scrypt-hardened) for a break.
    """
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    raw = kdf.derive((pin + settings.MASTER_PEPPER).encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def encrypt_secret(plaintext: str, pin: str) -> tuple[bytes, bytes]:
    """
    Encrypt a seed phrase or raw private key.
    Returns (ciphertext, salt) -- both are what you persist to the DB.
    Plaintext is never returned or logged.
    """
    salt = generate_salt()
    key = _derive_key(pin, salt)
    f = Fernet(key)
    ciphertext = f.encrypt(plaintext.encode("utf-8"))
    return ciphertext, salt


def decrypt_secret(ciphertext: bytes, salt: bytes, pin: str) -> str:
    """
    Decrypt. Raises DecryptionError on wrong PIN / tampering.
    Caller MUST scrub the returned string from memory ASAP after use
    (see services/session_keys.py for the in-memory TTL cache pattern).
    """
    key = _derive_key(pin, salt)
    f = Fernet(key)
    try:
        return f.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise DecryptionError("Wrong PIN or corrupted data") from e


def verify_pin(ciphertext: bytes, salt: bytes, pin: str) -> bool:
    try:
        decrypt_secret(ciphertext, salt, pin)
        return True
    except DecryptionError:
        return False


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())
