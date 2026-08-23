#!/usr/bin/env python3
"""add_account.py — register a Telegram account for UrkoCoin multi-account farming.

Usage:
  python add_account.py <name> <phone>
  e.g. python add_account.py alt1 +62812xxxxxxx

Flow: interactive OTP login via Telethon (2FA supported).
Session is saved to sessions/<name>.session and the account is appended to
accounts.json. TG_API_ID / TG_API_HASH come from ~/.tg_cred.env — those are
app-level credentials and are reusable across every account.
"""
import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

BOT_DIR = Path(__file__).parent
SESSION_DIR = BOT_DIR / 'sessions'
ACCOUNTS_FILE = BOT_DIR / 'accounts.json'
CRED_FILE = Path.home() / '.tg_cred.env'


def load_creds():
    if not CRED_FILE.exists():
        print(f'[!] {CRED_FILE} not found. Create it with:')
        print('    TG_API_ID=12345678')
        print('    TG_API_HASH=your_api_hash')
        print('    TG_PHONE=+62812xxxxxxxx')
        sys.exit(1)
    creds = {}
    for line in CRED_FILE.read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            creds[k.strip()] = v.strip()
    for key in ('TG_API_ID', 'TG_API_HASH'):
        if key not in creds:
            print(f'[!] {key} missing from {CRED_FILE}')
            sys.exit(1)
    return creds


def load_accounts():
    try:
        return json.loads(ACCOUNTS_FILE.read_text()).get('accounts', [])
    except Exception:
        return []


def save_accounts(accounts):
    ACCOUNTS_FILE.write_text(json.dumps({'accounts': accounts}, indent=2))
    ACCOUNTS_FILE.chmod(0o600)


async def login(name, phone, creds):
    SESSION_DIR.mkdir(exist_ok=True)
    sess = SESSION_DIR / name          # Telethon appends .session itself
    client = TelegramClient(str(sess), int(creds['TG_API_ID']), creds['TG_API_HASH'])
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f'[OK] session already authorized: '
                  f'@{me.username or me.first_name} ({me.id})')
            return me

        print(f'Sending OTP to {phone} ...')
        await client.send_code_request(phone)
        code = input('Enter OTP code: ').strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input('2FA password: ').strip()
            await client.sign_in(password=pw)

        me = await client.get_me()
        print(f'[OK] logged in: @{me.username or me.first_name} (id={me.id})')
        return me
    finally:
        await client.disconnect()


def main():
    if len(sys.argv) < 3:
        print('Usage: python add_account.py <name> <phone>')
        print('  e.g. python add_account.py alt1 +62812xxxxxxx')
        sys.exit(1)

    name = sys.argv[1].strip()
    phone = sys.argv[2].strip()
    creds = load_creds()

    accounts = load_accounts()
    if any(a.get('name') == name for a in accounts):
        print(f'[!] account "{name}" already registered — '
              f'a re-login will overwrite its session')

    me = asyncio.run(login(name, phone, creds))

    entry = {
        'name': name,
        'session': str(SESSION_DIR / f'{name}.session'),
        'phone': phone,
        'api_id': int(creds['TG_API_ID']),
        'api_hash': creds['TG_API_HASH'],
        'enabled': True,
    }
    accounts = [a for a in accounts if a.get('name') != name] + [entry]
    save_accounts(accounts)

    print(f'[OK] account "{name}" registered -> accounts.json (chmod 600)')
    print(f'     session: {SESSION_DIR / name}.session')
    print(f'     user:    @{me.username or me.first_name} (id={me.id})')
    print()
    print('IMPORTANT: open @urkocoin_bot once from this Telegram account and')
    print('press Start / open the mini app, so the bot can resolve the WebView')
    print('URL. Then run: .venv/bin/python urko_multi.py')


if __name__ == '__main__':
    main()
