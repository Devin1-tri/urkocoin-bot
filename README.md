# 🪙 UrkoCoin Multi-Account Farm Bot

Multi-account farming bot for **UrkoCoin** (`@urkocoin_bot`, Telegram Mini App).
Taps over the game's own WebSocket, claims every free reward, and buys only the
boosts that actually pay for themselves — all behind a live Rich terminal
dashboard.

> **Gold** is the in-game currency. Withdrawals are in **URKO**
> (500 gold = 1 URKO at the time of writing; the bot reads the live rate).
> Gold itself is **not** withdrawable, so the dashboard shows the gold rate
> converted to a rough USD/day figure to keep expectations honest.

---

## ✨ Features

- **Multi-account** — every Telegram account runs its own independent engine
  (own session, own socket, own state). Accounts never block each other.
- **Live Rich dashboard** — per-account gold, gold/hour, URKO, league, gold-per-click,
  taps, cycle count, plus a shared activity log.
- **Socket tapping** — taps are emitted over the game's Socket.IO channel and
  paced to energy regeneration, with automatic mid-cycle reconnect.
- **Auto claims** — daily dust, channel tasks, and the prize wheel (free spins only).
- **Economics-aware auto-upgrade** — buys `recharging` / `multiclick` only when
  measured payback is ≤ 7 days, after ≥ 2 hours of real data, max 2 buys per cycle.
  It will not touch the `energy` boost (useless for a bot that sits at 0 energy).
- **Self-healing auth** — Telegram `initData` expires every 5–15 minutes, so each
  engine refreshes it through Telethon every cycle and re-auths immediately on
  an HTTP 401/403.

---

## 📋 Requirements

| Requirement | Details |
|---|---|
| OS | Linux (tested on Ubuntu 22.04+) |
| Python | 3.11+ |
| RAM | ~120 MB for 5 accounts |
| Telegram API credentials | `api_id` + `api_hash` from https://my.telegram.org |
| `screen` | For persistent background runs |
| Terminal | ≥ 80 columns (the dashboard is laid out for 80 cols) |

---

## 🚀 Setup

### 1. Clone and install

```bash
git clone https://github.com/Devin1-tri/urkocoin-bot.git
cd urkocoin-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### 2. Telegram API credentials

Get `api_id` and `api_hash` from https://my.telegram.org → *API development tools*,
then create `~/.tg_cred.env`:

```env
TG_API_ID=12345678
TG_API_HASH=your_api_hash_here
TG_PHONE=+62812xxxxxxxx
```

```bash
chmod 600 ~/.tg_cred.env
```

These are **app-level** credentials — one pair works for every account you add.

### 3. Register your first account

```bash
./.venv/bin/python add_account.py main +62812xxxxxxxx
```

You will be prompted for the OTP code Telegram sends you (and for your 2FA
password if the account has one). The session is written to
`sessions/main.session` and the account is appended to `accounts.json`.

Repeat for each extra account:

```bash
./.venv/bin/python add_account.py alt1 +62813xxxxxxxx
./.venv/bin/python add_account.py alt2 +62814xxxxxxxx
```

### 4. Open the mini app once per account — required

For every account you added, open Telegram, go to **@urkocoin_bot**, press
**Start** and open the mini app once. The bot discovers the WebView URL from
that `/start` menu button and caches it in `webview_urls.json`. Without this
first manual open, `initData` cannot be minted and the account will sit at
status `NOINIT`.

### 5. Run

```bash
./.venv/bin/python urko_multi.py
```

Press `Ctrl+C` to stop.

---

## 🖥️ Running in the background (screen)

```bash
screen -dmS urko -T xterm-256color
screen -S urko -X stuff "cd $(pwd) && ./.venv/bin/python -u urko_multi.py\n"
```

Then:

```bash
screen -r urko          # attach (Ctrl+A then D to detach)
tail -f logs/urko.log   # follow the activity log without attaching
```

Snapshot the dashboard without attaching:

```bash
screen -S urko -X hardcopy /tmp/urko.txt && cat /tmp/urko.txt
```

---

## 📊 Reading the dashboard

```
╭──────────────────────────────────────────────────────────────╮
│ URKO MULTI 5/5ok      853kG 68k/h 0.0 URKO (~$46/d)  01:22:07│
╰──────────────────────────────────────────────────────────────╯
╭─────────────────────────── ACCOUNTS ─────────────────────────╮
│ Acct     │ St   │  Gold │  G/h │ URKO │ Lv   │ gpc │ Taps    │
│ main     │ TAP  │  508k │  28k │  0.0 │ Plat │  11 │ 238k    │
╰──────────────────────────────────────────────────────────────╯
╭─────────────────────────── ACTIVITY ─────────────────────────╮
│ [08:04:12] [main] dust +10,000 left=5                        │
╰──────────────────────────────────────────────────────────────╯
```

| Status | Meaning |
|---|---|
| `TAP` | Tapping over the socket (the money-making state) |
| `CHG` | Waiting for energy to regenerate |
| `DUST` / `TASKS` / `UPGR` | Claiming dust / channel tasks / evaluating boosts |
| `REFRESH` | Renewing `initData` between cycles |
| `REAUTH` | Server rejected the token mid-cycle; refreshing early |
| `IDLE` | Cycle finished, short pause |
| `NOINIT` | No `initData` — open the mini app manually (step 4) |
| `ERR` | Cycle raised; the engine retries automatically after 30 s |

---

## ⚙️ Tuning

All knobs live at the top of `urko_multi.py`:

| Constant | Default | What it does |
|---|---|---|
| `TAP_MIN` | `8` | Minutes of tapping per cycle |
| `TASK_CYCLE` | `4` | Check channel tasks every N cycles |
| `DUST_CYCLE` | `1` | Dust attempt every cycle (server enforces its own cooldown) |
| `WHEEL_CYCLE` | `2` | Check the wheel every N cycles |
| `UPGRADE_CYCLE` | `6` | Evaluate boosts every N cycles |
| `UPGRADE_RESERVE` | `20_000` | Gold kept liquid, never spent on boosts |
| `MAX_PAYBACK_DAYS` | `7` | Reject any boost slower than this to pay back |
| `MAX_BUYS_PER_CYCLE` | `2` | Spend cap per cycle |
| `MIN_UPGRADE_SPAN_H` | `2.0` | Minimum hours of data before any boost is bought |

To pause an account without deleting it, set `"enabled": false` for that entry
in `accounts.json`.

---

## 🧯 Troubleshooting

**`FATAL: no accounts in accounts.json`**
Copy the template and register an account:
`cp accounts.example.json accounts.json` then run `add_account.py`.

**Account stuck on `NOINIT`**
The WebView URL was never resolved. Open @urkocoin_bot in Telegram from *that*
account, press Start, open the mini app, then restart the bot. Delete
`webview_urls.json` if you want to force re-discovery.

**`dust err: HTTP 401 (launch-params stale)`**
Normal and self-healing — the token expired and the engine refreshes it on the
spot. If it repeats every cycle, the session is likely dead: re-run
`add_account.py <name> <phone>` for that account.

**`dust err: HTTP 5xx non-JSON: ...`**
Server-side hiccup. The bot logs it and continues; nothing to do.

**`tap connect err: ConnectionError`**
The game's socket refused the connection (usually a server restart or a stale
token). The engine retries next cycle.

**Dashboard looks garbled / boxes broken**
Your terminal is narrower than 80 columns, or `screen` was started without a
color-capable term. Always start it with `-T xterm-256color`.

**Nothing appears on screen and the process seems frozen**
Make sure you are on the current version. An older build called `print()` from
inside the event loop while Rich owned the screen, which could deadlock the
renderer. Logging now goes to `logs/urko.log` and only the dashboard writes to
the terminal.

**Telethon `database is locked`**
The same session file is open by another bot. Sessions here are meant to be
local copies under `sessions/` — never point `accounts.json` at a session file
another running bot uses.

---

## 📁 Project layout

| File | Purpose |
|---|---|
| `urko_multi.py` | **Main** — multi-account engine + Rich dashboard |
| `add_account.py` | Interactive OTP login, registers an account |
| `accounts.example.json` | Template to copy to `accounts.json` |
| `requirements.txt` | Python dependencies |
| `sessions/` | Telethon session files (git-ignored) |
| `logs/urko.log` | Activity log (git-ignored) |
| `webview_urls.json` | Cached mini-app URLs per account (git-ignored) |

---

## 🔐 Security notes

- `accounts.json`, `sessions/`, `webview_urls.json` and `~/.tg_cred.env` contain
  credentials that grant **full access to your Telegram accounts**. They are all
  git-ignored — keep them that way, and keep them `chmod 600`.
- A Telegram session file is as sensitive as a password. Never paste one into a
  chat, an issue, or a screenshot.
- The bot only talks to `api.urko.io` and Telegram's own servers.

## ⚠️ Disclaimer

Automating a Telegram mini app may violate its terms of service. Use at your own
risk, on accounts you own. No warranty; nothing here is financial advice.

## 📄 License

MIT
