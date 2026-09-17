"""Engångsscript: skapar Telegram-sessionen för läsarkontot.

Kör i valfri terminal med Python (t.ex. Google Cloud Shell, vilken som helst):

    pip3 install --user telethon
    python3 make_session.py

Logga in med läsarkontots nummer + koden som kommer i Telegram-appen.
Kopiera raden som skrivs ut och lägg den som GitHub-secret TG_SESSION.
Klistra ALDRIG in den någon annanstans — den ÄR inloggningen till kontot.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API ID (från my.telegram.org): ").strip())
api_hash = input("API HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as c:
    me = c.get_me()
    print(f"\nInloggad som {me.first_name} (@{me.username}).")
    print("\n=== DIN SESSION — lägg som GitHub-secret TG_SESSION: ===\n")
    print(c.session.save())
    print()
