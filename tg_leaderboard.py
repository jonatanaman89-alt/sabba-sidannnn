"""SABBA: kunderna ur Telegram (GitHub Actions → Supabase)
==========================================================
Sedan 2026-10-30 kommer köpen i SABBA från KVIQA (serverfunktionen kviqa i Supabase, varje minut). KVIQA lämnar aldrig
ut köparens namn, så det här skriptet läser bara KVIQsales-botens "New Sale"/"New Tip"-meddelanden i Telegram och lägger
dem i tabellen telegram_kop (köparen, modellen, beloppet, tiden). Databasen kopplar sedan varje meddelande till sitt köp i
KVIQA (telegram_koppla: samma modell, nära i tid och samma belopp) och sätter kunden på köpet — i säljflödet och arkivet.
Skriptet skriver ALDRIG säljflödet (sabba:chatters:v1) längre.

Varje körning hämtar bara meddelanden efter det senast lästa per chatt (telegram_senaste); första körningen läser
historiken bakåt (upp till 3000 per chatt och sökord), så att kunderna kommer på köpen sedan Telegram-läsaren stannade.

Miljövariabler (GitHub repo-secrets):
  TG_API_ID, TG_API_HASH   — från https://my.telegram.org
  TG_SESSION               — Telethon StringSession (skapas med make_session.py)
  SALES_BOT                — botens användarnamn, default "KVIQsalesBot"
  CHATTER_MAP              — "alias:Namn,alias2:Namn2" (Sent by-alias → teamnamn)
  SUPABASE_URL             — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY     — service_role-nyckeln (får BARA ligga som GitHub-secret)
"""

import os
import re
import sys

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# Exempel på meddelanden som parsas (sales OCH tips):
#   💰 New Sale: $12.00 (Dropfans, excl. VAT)
#   💸 New Tip: $400.00 (Dropfans, excl. VAT)
#   Platform: Telegram
#   Creator: Modellnamn
#   Sent by: chattaralias
#   Buyer: @kundnamn (Kund)
RE_SALE = re.compile(r"New (?:Sale|Tip):\s*\$\s*([\d.,]+)\s*(?:\(([^)]*)\))?", re.I)
RE_CREATOR = re.compile(r"^Creator:\s*(.+)$", re.I | re.M)
RE_SENTBY = re.compile(r"^Sent by:\s*(.+)$", re.I | re.M)
RE_BUYER = re.compile(r"^Buyer:\s*(.+)$", re.I | re.M)


def parse_sale(text):
    if not text:
        return None
    m = RE_SALE.search(text)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    paren = (m.group(2) or "").strip()
    cre = RE_CREATOR.search(text)
    sent = RE_SENTBY.search(text)
    buy = RE_BUYER.search(text)
    return {
        "amount": amount,
        "site": paren.split(",")[0].strip() if paren else "",
        "creator": cre.group(1).strip() if cre else "",
        "sent_by": sent.group(1).strip() if sent else "",
        "buyer": buy.group(1).strip() if buy else "",
    }


class Supa:
    """Tabellen telegram_kop och funktionerna telegram_senaste/telegram_koppla via REST (service_role)."""

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def rpc(self, namn, body=None):
        r = requests.post(f"{self.url}/rest/v1/rpc/{namn}", headers=self.headers, json=body or {}, timeout=60)
        r.raise_for_status()
        return r.json()

    def spara(self, rader):
        # Samma meddelande två gånger: det första står kvar (on conflict do nothing)
        for i in range(0, len(rader), 500):
            r = requests.post(f"{self.url}/rest/v1/telegram_kop", json=rader[i:i + 500], timeout=60,
                              headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"})
            r.raise_for_status()


def need_env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"FEL: secreten {name} är tom eller saknas — lägg in den under "
              f"Settings → Secrets and variables → Actions (exakt det namnet).")
        sys.exit(1)
    return v


def main():
    api_id = int(need_env("TG_API_ID"))
    api_hash = need_env("TG_API_HASH")
    session = need_env("TG_SESSION")
    bot_name = os.environ.get("SALES_BOT", "KVIQsalesBot").strip().lstrip("@").lower() or "kviqsalesbot"
    supa = Supa(need_env("SUPABASE_URL"), need_env("SUPABASE_SERVICE_KEY"))

    cmap = {}
    for pair in os.environ.get("CHATTER_MAP", "").split(","):
        if ":" in pair:
            a, b = pair.split(":", 1)
            cmap[a.strip().lower()] = b.strip()

    senaste = supa.rpc("telegram_senaste") or {}
    rader, errors = [], []

    # Anslut utan interaktiv inloggning — och ge ett BEGRIPLIGT fel om sessionen dött
    # (i stället för att Telethon frågar efter telefonnummer, vilket inte går här).
    client = TelegramClient(StringSession(session), api_id, api_hash)
    client.connect()
    if not client.is_user_authorized():
        print("FEL: Telegram-sessionen är utloggad eller ogiltig.")
        print("Åtgärd: kör make_session.py igen (Codespaces), kopiera den nya raden till")
        print("secreten TG_SESSION, och rör INTE 'Enheter' i Telegram-appen efteråt.")
        client.disconnect()
        sys.exit(1)
    with client:
        bot_id = None
        try:
            bot_id = client.get_entity(bot_name).id
        except Exception as e:
            errors.append(f"kunde inte slå upp @{bot_name}: {e}")

        for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            last = int(senaste.get(str(dialog.id), 0) or 0)
            limit = 3000 if last == 0 else 400
            try:
                for term in ("New Sale", "New Tip"):
                    for msg in client.iter_messages(dialog.id, limit=limit, min_id=last, search=term):
                        if bot_id is not None and msg.sender_id != bot_id:
                            continue
                        sale = parse_sale(msg.raw_text)
                        if not sale or not sale["creator"]:
                            continue
                        alias = sale["sent_by"]
                        rader.append({
                            "id": f"{dialog.id}:{msg.id}",
                            "dialog": dialog.id,
                            "msg": msg.id,
                            "t": msg.date.isoformat(),
                            "belopp": sale["amount"],
                            "saljare": cmap.get(alias.lower(), alias) or None,
                            "creator": sale["creator"],
                            "site": sale["site"] or None,
                            "kopare": sale["buyer"] or None,
                        })
            except Exception as e:
                errors.append(f"{dialog.name}: {e}")

    unika = list({r["id"]: r for r in rader}.values())
    if unika:
        supa.spara(unika)
    kopplade = supa.rpc("telegram_koppla", {"p_sek": 120})
    print(f"Klart: {len(unika)} meddelanden från Telegram, {kopplade} köp fick sin kund.")
    for e in errors:
        print("varning:", e)
    # Fel i enskilda chattar fäller inte körningen — men noll åtkomst alls ska synas rött.
    if bot_id is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
