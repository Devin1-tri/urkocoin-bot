#!/usr/bin/env python3
"""
urko_multi.py — Multi-account UrkoCoin farming dashboard.

Run: ./.venv/bin/python urko_multi.py   (from the repo root)
Stop: Ctrl+C

Architecture (learned the hard way, keep these rules):
  * Each account = one AccountEngine with its OWN state dict. No globals for
    per-account data — the single-account version used one global `state` and
    that design cannot grow.
  * Sessions are LOCAL COPIES under urkocoin/sessions/. The same Telegram
    accounts may be farming other bots too, and touching a session file while
    another process holds it risks sqlite locks. Never point this at another
    bot's session files.
  * initData TTL is ~5-15 min, so each engine refreshes via Telethon
    RequestWebView every cycle. The webview URL is discovered once via the
    /start button and then CACHED — sending /start every 8 minutes x5 accounts
    would invite floodwaits.
  * Socket auth needs userId as an INT. Passing a str connects but the server
    never pushes UPDATE_SCORE/SALDOS (silent zero-data bug, cost an hour).
  * Blocking REST calls run through asyncio.to_thread so 5 accounts don't
    serialize each other.
  * The tap loop reconnects on mid-cycle socket drops (the old abort-on-drop
    behavior cut cycles to ~40s and cost ~4.6x throughput).
  * Upgrades: tap-only measured rate, 2 h warm-up, max 2 buys/cycle, payback
    <= 7 days, 'energy' boost is NOT a candidate. See run_autoupgrade history.
"""
import asyncio, json, logging, re, sys, time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import unquote, parse_qs

from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich.console import Console

import socketio
import requests

HOME = Path.home()
API = 'https://api.urko.io/v1'
BOT_DIR = Path(__file__).parent
ACCOUNTS_FILE = BOT_DIR / 'accounts.json'
URL_CACHE_FILE = BOT_DIR / 'webview_urls.json'

# ─── Tuning constants (identical to urko_live.py — keep in sync) ─────
TAP_MIN = 8               # minutes of tapping per cycle
TASK_CYCLE = 4            # check tasks every N cycles
DUST_CYCLE = 1            # dust every cycle (server enforces its own cooldown)
UPGRADE_CYCLE = 6
WHEEL_CYCLE = 2

GOLD_PER_URKO = 500       # /exchange/getRate -> rateGoldUrko
GOLD_PER_USDT = 35_000    # /exchange/getRate -> rateGoldUsdt
NEW_WALLET_FEE_URKO = 2_500
UPGRADE_RESERVE = 20_000  # keep this much gold liquid

# Upgrade economics (measured 2026-08-22): energy regen is the bottleneck,
# taps track regen 1:1, so +1 recharge step = tap_rate/recharge per hour and
# +1 gpc step = tap_rate/gpc. 'energy' adds nothing (bot sits at ~0 energy).
UPGRADE_CANDIDATES = ('recharging', 'multiclick')
MAX_PAYBACK_DAYS = 7
MAX_BUYS_PER_CYCLE = 2

MIN_RATE_SPAN_H = 0.5     # show gold/hour after 30 min
MIN_EMA_SPAN_H = 0.15
MIN_UPGRADE_SPAN_H = 2.0  # never buy a boost on less than 2 h of data

LEVEL_NAMES = {
    'wood': 'Wood', 'bronze': 'Bronze', 'silver': 'Silver',
    'gold': 'Gold', 'platinum': 'Plat', 'diamond': 'Dia',
    'master': 'Mast', 'grandmaster': 'GM',
    'elite': 'Elite', 'legendary': 'Leg', 'mythic': 'Myth',
}

# ─── Shared log ring ─────────────────────────────────────────────────
# CRITICAL: never print() while a Rich Live(screen=True) is active.
# Rich wraps sys.stdout in a FileProxy; a print() from inside the event loop
# triggers a nested Live re-render. If anything raises during that render
# (e.g. aiohttp's unclosed-connector __del__ -> asyncio exception handler ->
# logging -> FileProxy -> print again) Rich re-enters its own renderer and the
# whole process wedges with no traceback. That is exactly what froze this bot
# at 05:43 on 2026-08-23. Log to the ring + a plain file instead; the dashboard
# renders the ring, and the file survives restarts.
LOG_LINES = []
LOG_FILE = BOT_DIR / 'logs' / 'urko.log'
LIVE_ACTIVE = False


def glog(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    if len(LOG_LINES) > 30:
        LOG_LINES[:] = LOG_LINES[-30:]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open('a') as f:
            f.write(line + '\n')
    except Exception:
        pass
    if not LIVE_ACTIVE:
        # only safe before Live starts / after it exits
        print(line, flush=True)


# ─── Account registry ────────────────────────────────────────────────
@dataclass
class Account:
    name: str
    session: str
    phone: str = ''
    api_id: int = 0
    api_hash: str = ''
    enabled: bool = True

    def session_path(self) -> Path:
        p = Path(self.session).expanduser()
        return p.with_suffix('') if p.suffix == '.session' else p


def load_accounts():
    data = json.loads(ACCOUNTS_FILE.read_text())
    return [Account(**a) for a in data.get('accounts', []) if a.get('enabled', True)]


def load_url_cache():
    try:
        return json.loads(URL_CACHE_FILE.read_text())
    except Exception:
        return {}


def save_url_cache(cache):
    try:
        URL_CACHE_FILE.write_text(json.dumps(cache, indent=2))
        URL_CACHE_FILE.chmod(0o600)
    except Exception:
        pass


async def _hard_close(sio):
    """Fully tear down a socketio.AsyncClient.

    `await sio.disconnect()` leaves python-engineio's aiohttp ClientSession
    open. Its connector then complains from __del__ ("Unclosed connector") via
    the asyncio exception handler -> logging -> stdout, which is fatal under a
    Rich Live screen (see the glog note). Closing the session here removes the
    warning at the source instead of just muting it.
    """
    try:
        await sio.disconnect()
    except BaseException:
        pass
    for attr in ('http', '_http'):
        sess = getattr(getattr(sio, 'eio', None), attr, None)
        if sess is not None and not getattr(sess, 'closed', True):
            try:
                await sess.close()
            except BaseException:
                pass


def fmt_short(n):
    """Compact number for the 80-col dashboard."""
    try:
        n = float(n)
    except Exception:
        return '?'
    if n >= 1_000_000:
        return f'{n/1_000_000:.2f}M'
    if n >= 10_000:
        return f'{n/1_000:.0f}k'
    if n >= 1_000:
        return f'{n/1_000:.1f}k'
    return f'{n:.0f}'


# ─── Per-account engine ──────────────────────────────────────────────
class AccountEngine:
    def __init__(self, account: Account, url_cache: dict):
        self.acc = account
        self.url_cache = url_cache
        self.raw = None            # current initData (raw, no 'tma ' prefix)
        self.uid = None            # int!
        self.auth_stale = False    # set by _post on 401/403 -> refresh early
        self.alive = True
        now = time.time()
        self.state = {
            'gold': 0, 'energy': 0, 'max_energy': 1000,
            'gpc': 1, 'recharge': 1, 'level': '?',
            'urko': 0, 'taps_session': 0, 'cycles': 0,
            'status': 'starting', 'dust_left': '?',
            'gold_start': None, 'gold_start_at': 0,
            'gold_rate_h': 0.0, 'gold_span_h': 0.0,
            'gold_rate_ema': 0.0, 'last_gold': 0, 'last_gold_at': 0,
            'claim_gold': 0, 'tap_rate_h': 0.0, 'session_spent': 0,
            'free_spins': 0, 'spin_cost': 0, 'wheel_used': 0, 'wheel_cap': 0,
            'boosts': {}, 'last_refresh': '', 'started_at': now,
        }

    def log(self, msg):
        glog(f"[{self.acc.name}] {msg}")

    # ── measurement (same math as urko_live.py) ──
    def note_claim(self, amount):
        try:
            amount = int(amount or 0)
        except Exception:
            return
        if amount > 0:
            self.state['claim_gold'] += amount

    def note_gold(self, gold):
        if not isinstance(gold, (int, float)) or gold <= 0:
            return
        st = self.state
        now = time.time()
        if st['gold_start'] is None:
            st['gold_start'] = gold
            st['gold_start_at'] = now
            st['last_gold'] = gold
            st['last_gold_at'] = now
            return
        span_h = (now - st['gold_start_at']) / 3600.0
        st['gold_span_h'] = span_h
        if span_h >= MIN_RATE_SPAN_H:
            earned = (gold + st['session_spent']) - st['gold_start']
            st['gold_rate_h'] = earned / span_h
            st['tap_rate_h'] = max(0.0, (earned - st['claim_gold']) / span_h)
        dt_h = (now - st['last_gold_at']) / 3600.0
        if dt_h >= MIN_EMA_SPAN_H:
            inst = (gold - st['last_gold']) / dt_h
            if inst >= 0:
                prev = st['gold_rate_ema']
                st['gold_rate_ema'] = inst if prev <= 0 else prev * 0.7 + inst * 0.3
            st['last_gold'] = gold
            st['last_gold_at'] = now

    # ── initData via Telethon (async-native) ──
    async def refresh_init(self):
        from telethon import TelegramClient, functions, types
        cl = TelegramClient(str(self.acc.session_path()),
                            self.acc.api_id, self.acc.api_hash)
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                self.log('session not authorized')
                return False
            bot = await cl.get_entity('urkocoin_bot')
            url = self.url_cache.get(self.acc.name)
            if not url:
                # discover once via the /start menu button, then cache
                try:
                    await cl.send_message(bot, '/start')
                    await asyncio.sleep(2)
                    async for m in cl.iter_messages(bot, limit=3):
                        if not m.reply_markup:
                            continue
                        for row in m.reply_markup.rows:
                            for btn in row.buttons:
                                if isinstance(btn, types.KeyboardButtonWebView):
                                    url = btn.url
                except Exception:
                    pass
                if url:
                    self.url_cache[self.acc.name] = url
                    save_url_cache(self.url_cache)
            kwargs = dict(peer=bot, bot=bot, platform='android', from_bot_menu=False)
            if url:
                kwargs['url'] = url
            result = await cl(functions.messages.RequestWebViewRequest(**kwargs))
            m = re.search(r'tgWebAppData=([^&]+)', result.url)
            if not m:
                self.log('no tgWebAppData in webview url')
                return False
            self.raw = unquote(m.group(1))
            self.uid = json.loads(parse_qs(self.raw)['user'][0])['id']  # int!
            self.state['last_refresh'] = time.strftime('%H:%M:%S')
            return True
        except Exception as e:
            self.log(f'init refresh err: {e.__class__.__name__} {str(e)[:80]}')
            return False
        finally:
            try:
                await cl.disconnect()
            except Exception:
                pass

    def _headers(self):
        return {'launch-params': self.raw, 'content-type': 'application/json'}

    def _post(self, path, body=None, timeout=25):
        """POST + JSON decode with a useful error.

        The API answers non-JSON (empty body / HTML / plain text) when
        launch-params are stale or a proxy hiccups. Bare `.json()` then raised
        "Expecting value: line 1 column 1 (char 0)", which says nothing about
        WHY. Return (payload, err) and include the HTTP status so the log line
        is actionable, and flag stale auth so the cycle can refresh initData.
        """
        try:
            r = requests.post(API + path, headers=self._headers(),
                              json=body if body is not None else {}, timeout=timeout)
        except Exception as e:
            return None, f'{e.__class__.__name__}'
        if r.status_code in (401, 403):
            self.auth_stale = True
            return None, f'HTTP {r.status_code} (launch-params stale)'
        try:
            return r.json(), None
        except Exception:
            body_snip = (r.text or '')[:60].replace('\n', ' ')
            if r.status_code >= 400:
                self.auth_stale = True
            return None, f'HTTP {r.status_code} non-JSON: {body_snip!r}'

    # ── blocking REST actions (run via asyncio.to_thread) ──
    def run_tasks(self):
        d, err = self._post('/tasks/get')
        if err:
            self.log(f'tasks err: {err}')
            return
        try:
            H = self._headers()
            tasks = d.get('payload', {}).get('channels', [])
            claimed = 0
            for t in tasks:
                r2 = requests.post(API + '/tasks/check', headers=H,
                                   json={'id': t['id']}, timeout=25)
                try:
                    p = r2.json().get('payload', '')
                except Exception:
                    continue
                if p == 'ok':
                    claimed += 1
                    self.note_claim(t.get('award', 0))
                    self.log(f"task [{t['id']}] +{t['award']:,}")
                time.sleep(1)
            if claimed:
                self.log(f'tasks: {claimed} claimed')
        except Exception as e:
            self.log(f'tasks err: {e.__class__.__name__} {str(e)[:50]}')

    def run_dust(self):
        d, err = self._post('/user/dust')
        if err:
            self.log(f'dust err: {err}')
            return
        try:
            p = d.get('payload', {}) or {}
            if p.get('ok'):
                self.state['dust_left'] = p.get('quedanHoy', '?')
                self.note_claim(p.get('premio', 0))
                if p.get('gold'):
                    self.note_gold(p['gold'])
                    self.state['gold'] = p['gold']
                self.log(f"dust +{p.get('premio',0):,} left={p.get('quedanHoy')}")
            elif p.get('esperaSeg') is not None:
                self.state['dust_left'] = f"{p['esperaSeg']//60}m"
        except Exception as e:
            self.log(f'dust err: {e.__class__.__name__} {str(e)[:50]}')

    def run_wheel(self):
        """Spin ONLY when the server grants a free spin. The wheel is a gold
        SINK (cost 5,000; 'tope' is the hourly LIMIT, not free spins)."""
        H = self._headers()
        st = self.state
        d, err = self._post('/wheel/current')
        if err:
            self.log(f'wheel err: {err}')
            return
        p = d.get('payload', {}) or {}
        st['free_spins'] = int(p.get('freeSpins') or 0)
        st['spin_cost'] = int(p.get('cost') or 0)
        st['wheel_used'] = int(p.get('usados') or 0)
        st['wheel_cap'] = int(p.get('tope') or 0)
        if not p.get('activa'):
            return
        spun = 0
        room = max(0, st['wheel_cap'] - st['wheel_used']) if st['wheel_cap'] else 99
        while st['free_spins'] > 0 and spun < room:
            try:
                r2 = requests.post(API + '/wheel/spin', headers=H, json={}, timeout=25)
                d2 = r2.json()
            except Exception as e:
                self.log(f'spin err: {e}')
                break
            if d2.get('error') or r2.status_code != 200:
                break
            res = d2.get('payload', d2) or {}
            prize = res.get('prize', '?')
            for tag, amt in (('gold2000', 2000), ('gold5000', 5000),
                             ('gold10000', 10000), ('gold25000', 25000)):
                if str(prize) == tag:
                    self.note_claim(amt)
                    break
            if res.get('gold'):
                self.note_gold(res['gold'])
                st['gold'] = res['gold']
            st['free_spins'] = int(res.get('freeSpins', st['free_spins'] - 1) or 0)
            spun += 1
            self.log(f'FREE spin -> {prize} left={st["free_spins"]}')
            time.sleep(1.5)

    def run_autoupgrade(self):
        """Buy boosts by MEASURED tap payback. History (do not regress):
        1) budget=gold*0.5 never fired; 2) gpc*recharge*3600 was 37x too
        optimistic; 3) a 30-min window containing one dust claim triggered a
        587k spend in 3s, 90k of it on useless 'energy'. Now: tap-only rate,
        2 h warm-up, <=2 buys/cycle, payback <=7d, no 'energy'."""
        H = self._headers()
        st = self.state
        d, err = self._post('/boosts/get')
        if err:
            self.log(f'upgrade err: {err}')
            return
        try:
            boosts = {b['id']: b for b in d.get('payload', [])}
        except Exception as e:
            self.log(f'upgrade err: {e.__class__.__name__} {str(e)[:50]}')
            return
        st['boosts'] = {k: {'level': v.get('level'), 'cost': v.get('updateCost')}
                        for k, v in boosts.items()}
        if st['gold_span_h'] < MIN_UPGRADE_SPAN_H:
            return
        tap_h = st['tap_rate_h']
        if tap_h <= 0:
            return
        bought = 0
        seen_cost = {}
        for _ in range(MAX_BUYS_PER_CYCLE):
            budget = st['gold'] - UPGRADE_RESERVE
            if budget <= 0:
                break
            cands = []
            for bid in UPGRADE_CANDIDATES:
                b = boosts.get(bid) or {}
                cost = b.get('updateCost', -1)
                if not cost or cost < 0 or cost > budget:
                    continue
                if bid in seen_cost and seen_cost[bid] == cost:
                    continue    # no-progress guard
                if bid == 'recharging':
                    gain_h = tap_h / max(1, st['recharge'])
                elif bid == 'multiclick':
                    gain_h = tap_h / max(1, st['gpc'])
                else:
                    continue
                if gain_h <= 0:
                    continue
                cands.append((cost / gain_h, bid, cost, gain_h))
            if not cands:
                break
            cands.sort()
            payback_h, name, cost, gain_h = cands[0]
            if payback_h > 24 * MAX_PAYBACK_DAYS:
                self.log(f'skip {name}: payback {payback_h/24:.1f}d '
                         f'(cost {cost:,}, tap {tap_h:,.0f}/h)')
                break
            try:
                r = requests.post(API + '/boosts/buy', headers=H,
                                  json={'boost': name}, timeout=25)
                d2 = r.json()
            except Exception:
                break
            if r.status_code == 200 and d2.get('status') == 'ok':
                bought += 1
                seen_cost[name] = cost
                st['gold'] -= cost
                st['session_spent'] += cost
                self.log(f'{name} -{cost:,} payback {payback_h/24:.1f}d '
                         f'+{gain_h:,.0f}/h')
                pl = d2.get('payload', {}) or {}
                if 'newGoldPerClick' in pl:
                    st['gpc'] = pl['newGoldPerClick']
                if 'rechargingEnergy' in pl:
                    st['recharge'] = pl['rechargingEnergy']
                try:
                    d3 = requests.post(API + '/boosts/get', headers=H,
                                       json={}, timeout=25).json()
                    boosts = {b['id']: b for b in d3.get('payload', [])}
                except Exception:
                    break
                time.sleep(1)
            else:
                break
        if bought:
            self.log(f'autoupgrade: {bought} bought gpc={st["gpc"]} '
                     f'rech={st["recharge"]}/s spent={st["session_spent"]:,}')

    # ── tap loop (async, reconnect-on-drop) ──
    async def tap_loop(self, minutes):
        st = self.state
        end = time.time() + minutes * 60
        taps = 0
        batch = 20
        reconnects = 0
        MAX_RECONNECTS = 4

        async def connect():
            s = socketio.AsyncClient(reconnection=False)

            @s.on('UPDATE_SCORE')
            async def on_score(d):
                self.note_gold(d.get('gold'))
                st['gold'] = d.get('gold', st['gold'])
                st['energy'] = d.get('energyLeft', st['energy'])
                st['gpc'] = d.get('goldPerClick', st['gpc'])
                st['recharge'] = d.get('rechargingEnergyPerSecond', st['recharge'])
                if d.get('dailyEnergy'):
                    st['max_energy'] = d['dailyEnergy']
                if d.get('level'):
                    st['level'] = d['level']

            @s.on('SALDOS')
            async def on_saldos(d):
                st['urko'] = d.get('urko', st['urko'])
                st['free_spins'] = d.get('freeSpins', st['free_spins'])
                self.note_gold(d.get('gold'))
                if d.get('gold'):
                    st['gold'] = d['gold']

            await s.connect('https://api.urko.io',
                            auth={'launchParams': self.raw, 'userId': self.uid,
                                  'version': 'v1'},
                            transports=['websocket'], wait_timeout=15)
            return s

        try:
            sio = await connect()
        except BaseException as e:
            self.log(f'tap connect err: {e.__class__.__name__}')
            st['status'] = 'ERR sock'
            return 0

        st['status'] = 'TAP'
        await asyncio.sleep(2)
        while time.time() < end:
            if not sio.connected:
                if reconnects >= MAX_RECONNECTS:
                    self.log(f'socket dropped {reconnects}x, ending cycle')
                    break
                reconnects += 1
                st['status'] = f'RECON{reconnects}'
                await _hard_close(sio)
                await asyncio.sleep(2)
                try:
                    sio = await connect()
                    await asyncio.sleep(1)
                    st['status'] = 'TAP'
                except BaseException as e:
                    self.log(f'reconnect failed: {e.__class__.__name__}')
                    break
                continue
            if st['energy'] >= batch:
                try:
                    await sio.emit('CLICKS', {'count': batch})
                except BaseException:
                    continue
                taps += batch
                st['taps_session'] += batch
                st['energy'] -= batch
                st['status'] = 'TAP'
                await asyncio.sleep(0.5)
            else:
                wait = max(1, (batch - st['energy']) // max(1, st['recharge']))
                wait = min(wait, 30)
                st['status'] = 'CHG'
                await asyncio.sleep(wait)
                # server pushes no periodic energy updates — add regen locally
                st['energy'] = min(st['max_energy'],
                                   st['energy'] + int(st['recharge'] * wait))
        if reconnects:
            self.log(f'cycle survived {reconnects} reconnect(s)')
        await _hard_close(sio)
        return taps

    # ── per-account cycle loop ──
    async def run_forever(self):
        cycle = 0
        await self.refresh_init()
        while self.alive:
            try:
                cycle += 1
                self.state['cycles'] = cycle
                if not self.raw:
                    self.state['status'] = 'NOINIT'
                    await asyncio.sleep(30)
                    await self.refresh_init()
                    continue

                if cycle == 1 or cycle % TASK_CYCLE == 0:
                    self.state['status'] = 'TASKS'
                    await asyncio.to_thread(self.run_tasks)

                self.state['status'] = 'DUST'
                await asyncio.to_thread(self.run_dust)

                # A 401/403 means launch-params expired mid-cycle. Refresh NOW
                # instead of burning the whole tap window on a dead token.
                if self.auth_stale:
                    self.auth_stale = False
                    self.state['status'] = 'REAUTH'
                    self.log('launch-params stale -> refreshing initData')
                    await self.refresh_init()

                if cycle == 1 or cycle % WHEEL_CYCLE == 0:
                    await asyncio.to_thread(self.run_wheel)

                if cycle == 1 or cycle % UPGRADE_CYCLE == 0:
                    self.state['status'] = 'UPGR'
                    await asyncio.to_thread(self.run_autoupgrade)

                taps = await self.tap_loop(TAP_MIN)
                st = self.state
                rate = st['gold_rate_h']
                self.log(f'cycle {cycle}: {taps} taps gold={st["gold"]:,}'
                         + (f' {rate:,.0f}/h' if rate > 0 else ''))
                st['status'] = 'IDLE'
                await asyncio.sleep(2)

                # initData TTL ~5-15 min; cycle ~8 min -> refresh every cycle
                self.state['status'] = 'REFRESH'
                ok = await self.refresh_init()
                if not ok:
                    self.log('init refresh failed, will retry next cycle')
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                # One account must never take the process down. Before this
                # guard an unexpected error inside a cycle propagated through
                # asyncio.gather and killed every engine at once.
                self.state['status'] = 'ERR'
                self.log(f'cycle {cycle} crashed: {e.__class__.__name__} {str(e)[:60]}')
                await asyncio.sleep(30)
                self.auth_stale = False
                try:
                    await self.refresh_init()
                except BaseException:
                    pass


# ─── Dashboard ───────────────────────────────────────────────────────
ENGINES: list = []
START_TIME = time.time()


def make_dashboard():
    layout = Layout()
    layout.split_column(
        Layout(name='header', size=3),
        Layout(name='accounts', size=11),
        Layout(name='log'),
    )

    tot_gold = sum(e.state['gold'] for e in ENGINES)
    tot_rate = sum(e.state['gold_rate_h'] for e in ENGINES)
    tot_urko = sum(e.state['urko'] for e in ENGINES)
    ok_n = sum(1 for e in ENGINES if e.raw)
    up_s = time.time() - START_TIME
    hh, rem = divmod(int(up_s), 3600)
    mm, ss = divmod(rem, 60)

    hdr = Table.grid(expand=True)
    hdr.add_column(justify='left')
    hdr.add_column(justify='center')
    hdr.add_column(justify='right')
    hdr.add_row(
        f'[bold cyan]URKO MULTI[/] {ok_n}/{len(ENGINES)}ok',
        f'[gold1]{fmt_short(tot_gold)}G[/] '
        f'[green]{fmt_short(tot_rate)}/h[/] '
        f'[magenta]{tot_urko:,.1f} URKO[/] '
        f'(~${tot_rate*24/GOLD_PER_USDT:.2f}/d)',
        f'[dim]{hh:02d}:{mm:02d}:{ss:02d}[/]',
    )
    layout['header'].update(Panel(hdr, border_style='cyan'))

    t = Table(expand=True, pad_edge=False)
    t.add_column('Acct', width=9, no_wrap=True)
    t.add_column('St', width=6, no_wrap=True)
    t.add_column('Gold', justify='right', width=8, no_wrap=True)
    t.add_column('G/h', justify='right', width=7, no_wrap=True)
    t.add_column('URKO', justify='right', width=8, no_wrap=True)
    t.add_column('Lv', width=6, no_wrap=True)
    t.add_column('gpc', justify='right', width=4, no_wrap=True)
    t.add_column('Taps', justify='right', width=7, no_wrap=True)
    t.add_column('Cyc', justify='right', width=4, no_wrap=True)
    for e in ENGINES:
        st = e.state
        rate = st['gold_rate_h']
        rate_s = fmt_short(rate) if rate > 0 else '…'
        lvl = LEVEL_NAMES.get(str(st['level']).lower(), str(st['level'])[:6])
        status = st['status']
        style = 'green' if status == 'TAP' else ('yellow' if status in ('CHG', 'IDLE') else 'red' if 'ERR' in status or status == 'NOINIT' else 'cyan')
        t.add_row(
            e.acc.name,
            Text(status, style=style),
            fmt_short(st['gold']),
            rate_s,
            f"{st['urko']:,.1f}",
            lvl,
            str(st['gpc']),
            fmt_short(st['taps_session']),
            str(st['cycles']),
        )
    layout['accounts'].update(Panel(t, title='ACCOUNTS', border_style='green'))

    log_text = '\n'.join(LOG_LINES[-12:]) if LOG_LINES else 'starting...'
    layout['log'].update(Panel(Text(log_text, no_wrap=True, overflow='ellipsis'),
                               title='ACTIVITY', border_style='blue'))
    return layout


# ─── Main ────────────────────────────────────────────────────────────
async def main():
    global ENGINES
    accounts = load_accounts()
    if not accounts:
        glog('FATAL: no accounts in accounts.json')
        return
    url_cache = load_url_cache()
    ENGINES = [AccountEngine(a, url_cache) for a in accounts]
    glog(f'=== URKO MULTI START: {len(ENGINES)} accounts ===')

    console = Console()
    tasks = []
    # stagger starts: no burst of 5 simultaneous Telethon+socket connects
    for i, e in enumerate(ENGINES):
        tasks.append(asyncio.create_task(e.run_forever()))
        await asyncio.sleep(5)

    # Silence asyncio's default exception handler while Live owns the screen.
    # aiohttp's connector __del__ logs "Unclosed connector" via logging ->
    # stdout -> Rich FileProxy -> nested render -> deadlock (see glog note).
    loop = asyncio.get_running_loop()

    def _quiet_handler(_loop, context):
        glog(f"async warn: {str(context.get('message'))[:70]}")

    loop.set_exception_handler(_quiet_handler)
    logging.getLogger('asyncio').setLevel(logging.CRITICAL)
    logging.getLogger('aiohttp').setLevel(logging.CRITICAL)
    logging.getLogger('engineio.client').setLevel(logging.CRITICAL)
    logging.getLogger('socketio.client').setLevel(logging.CRITICAL)

    # get_renderable= callback so Rich re-renders every frame (2x/sec).
    # Passing make_dashboard() directly evaluates it ONCE and the screen freezes.
    global LIVE_ACTIVE
    LIVE_ACTIVE = True
    try:
        with Live(get_renderable=make_dashboard, console=console,
                  refresh_per_second=2, screen=True):
            try:
                await asyncio.gather(*tasks)
            except KeyboardInterrupt:
                for e in ENGINES:
                    e.alive = False
    finally:
        LIVE_ACTIVE = False


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
