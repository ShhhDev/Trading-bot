# Solana + TON Trading Bot (Telegram)

Non-custodial-leaning Telegram trading bot: chain selection, wallet
import/create, live balances & holdings, market/limit/TP/SL orders, swaps,
watchlist with % move alerts, and multi-wallet copy trading.

## Security model (important)

- **Nothing is stored in plaintext.** Seed phrases / private keys are
  encrypted with Fernet using a key derived (scrypt) from `PIN + server
  pepper + per-wallet salt`. The PIN itself is **never stored** anywhere.
- **A DB leak alone is not enough to steal funds** — an attacker would also
  need the `MASTER_PEPPER` (kept out of the DB, in your secrets manager) and
  would still have to brute-force each user's PIN through scrypt.
- **Decrypted keys live in memory only**, in `services/session_keys.py`,
  with a 15-minute TTL, keyed per (user, wallet). A process restart wipes
  the cache. This is what lets a user tap Buy/Sell repeatedly without
  retyping their PIN every time.
- **There is no PIN recovery.** If a user loses their PIN and doesn't have
  their own separate backup of the seed, funds in a bot-managed wallet are
  gone. This must be disclosed to users up front (the onboarding copy
  already does this — don't remove it).
- **Recommended default for users:** generate a new bot-managed wallet and
  only fund it with trading-sized amounts, rather than importing an existing
  wallet holding significant funds. Importing is supported (per your
  request) but carries materially more risk and the UI shows an explicit
  warning gate before it's allowed.

## Project layout

```
config.py                    Env-driven settings (pydantic-settings)
main.py                      Entry point: wires routers + APScheduler jobs
states.py                    All aiogram FSM states

models/db.py                 SQLAlchemy models (User, Wallet, Order,
                              WatchlistItem, CopyTradeConfig) + async engine

services/
  crypto_vault.py             Encryption/decryption core (READ THIS FILE)
  session_keys.py             In-memory TTL cache for unlocked keys
  wallet_service.py           Mnemonic gen/validation, SOL/TON key derivation
  solana_client.py            RPC reads, Jupiter quote+swap
  ton_client.py               tonapi reads; swap execution stubbed
  order_engine.py             Market order execution; limit/TP/SL trigger checks
  copy_trade_engine.py        Decision logic for notify/auto-copy
  watchlist_engine.py         % move alert checker

handlers/
  onboarding.py                /start -> chain select -> import/create -> PIN
  wallet_menu.py               My Wallet: balance, holdings, deposit, slippage
  trading.py                   Buy / Sell / Limit / TP / SL
  swap.py                      CA -> CA swap flow
  watchlist.py                 Add/remove tokens, set % alerts
  copy_trade.py                Add/manage followed trader wallets
  unlock.py                    PIN entry -> populates session_keys cache

keyboards/
  inline.py                    Every inline keyboard in the app
  reply.py                     The 3 persistent menu buttons
```

## The 3 persistent menu buttons

As requested, `keyboards/reply.py` defines a persistent reply keyboard with
exactly three buttons — **My Wallet**, **Copy Trade**, **Watchlist** — each
of which opens its full inline-button UI (all actual actions are inline
buttons, per your spec; the reply keyboard is just quick navigation).

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in BOT_TOKEN, MASTER_PEPPER (generate via the command in .env.example),
# SOLANA_RPC_URL (use Helius/QuickNode in production, not the public RPC),
# TON_API_KEY, etc.

python main.py
```

## What's fully wired vs. stubbed

| Feature | Status |
|---|---|
| Chain selection (SOL/TON/Both) | ✅ Wired |
| Wallet create (both chains) | ✅ Wired |
| Wallet import + validation | ✅ Wired |
| PIN encryption/decryption | ✅ Wired |
| Session unlock (buy/sell without re-PIN) | ✅ Wired |
| SOL balance / holdings | ✅ Wired (public RPC by default — set HELIUS_API_KEY + point SOLANA_RPC_URL at Helius for production; a startup warning reminds you) |
| TON balance / holdings | ✅ Wired (tonapi.io) |
| Market Buy/Sell (Solana, via Jupiter) | ✅ Wired |
| Market Buy/Sell (TON, via STON.fi) | ⚠️ Structurally complete, **not network-verified** — see the warning at the top of `ton_client.py`. Test on testnet before trusting with funds. |
| Limit orders | ✅ Wired (polling-based trigger check every 10s) |
| Take Profit / Stop Loss (%-based, sized off real holdings) | ✅ Wired |
| Swap (CA → CA) | ✅ Wired for SOL; TON leg depends on the same STON.fi integration above |
| CA/address validation on input | ✅ Wired (structural validation for SOL pubkeys and TON addresses) |
| Token symbol display (not raw addresses) | ✅ Wired (Helius DAS / Jupiter token list for SOL, tonapi for TON, cached) |
| Slippage control | ✅ Wired |
| Trade guardrails (max per-trade $, max daily volume $, confirm-above threshold) | ✅ Wired — enforced in the single execution choke point, covers manual + limit/TP-SL + copy-trade |
| Deposit detection | ✅ Wired (polling-based; currently notifies on "new wallet activity" generically rather than specifically labeling incoming vs outgoing — see `deposit_watcher.py` docstring) |
| Watchlist + % pump/dump alerts | ✅ Wired |
| Copy trade config (multi-wallet, notify/auto, min size, $ or % sizing) | ✅ Wired |
| Copy trade **event detection** | ✅ Wired — polling-based (`copy_trade_poller.py`), uses Helius enhanced-tx parsing when `HELIUS_API_KEY` is set, falls back to raw balance-delta parsing otherwise. TON side is a structural placeholder (see that file's docstring) since tonapi's swap-labeled events endpoint needs the same live-verification caveat as STON.fi above. |
| Copy trade auto-execution | ✅ Wired — requires the user's wallet to be unlocked (PIN) since there's no one present to authorize at 3am; fails gracefully with a clear notification if locked |
| Large-amount buys | ✅ No hard cap by default; guardrails let users (or you, platform-wide) set one |

## Known gaps / next steps to take this to real production

1. **Verify STON.fi's API shape live.** I do not have network access in this
   environment to confirm `ask_units`, `router_address`, `swap_body`, and
   `forward_amount` are still STON.fi's exact current response field names.
   Curl their `/v1/swap/simulate` endpoint with a real pair before trusting
   `ton_client.py`'s swap execution. Same caveat for the TON side of
   copy-trade parsing in `copy_trade_poller.py`.
2. **Deposit/activity detection is generic, not specific.** It tells the user
   "new activity on your wallet" rather than "0.5 SOL deposited from X" —
   parsing pre/post balance deltas the way `copy_trade_poller.py`'s raw-RPC
   fallback does would get you there; it's the same technique, just not
   applied to the deposit watcher yet.
3. **Consider webhooks over polling once you have inbound HTTP.** Both
   `copy_trade_poller.py` and `deposit_watcher.py` are polling-based by
   design (works on any host, no public URL needed). Helius/tonapi webhooks
   would cut latency and RPC load once you're ready to stand up a receiver.
4. **Add token metadata to TON's price/decimals lookups more robustly** —
   current TON decimal lookups for sells depend on the wallet already
   holding the token (to read decimals from `get_jetton_holdings`); a token
   metadata endpoint call would remove that dependency.
5. **Security review** of `crypto_vault.py` and `session_keys.py` specifically
   before any real funds touch this — this note isn't going away regardless
   of how much else gets built out.
