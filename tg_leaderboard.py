"""SABBA Chattar-leaderboard (GitHub Actions → Supabase)
========================================================
Loggar in som teamets läsarkonto (Telethon), skannar KVIQsales-botens
"New Sale"-meddelanden och räknar ihop VECKOTOTALER PER CHATTARE (fältet
"Sent by:"). Skriver till Supabase-tabellen sabba_data, nyckeln
sabba:chatters:v1 — 🏆-toppen på Sales-sidan uppdateras live via realtidssynken.

Startas var 10:e minut av Supabase (pg_cron → workflow_dispatch). Håller reda på senast
lästa meddelande per chatt ("last") så varje körning bara hämtar det nya —
första körningen backfyller historiken.

Miljövariabler (GitHub repo-secrets):
  TG_API_ID, TG_API_HASH   — från https://my.telegram.org
  TG_SESSION               — Telethon StringSession (skapas med make_session.py)
  SALES_BOT                — botens användarnamn, default "KVIQsalesBot"
  CHATTER_MAP              — "alias:Namn,alias2:Namn2" (Sent by-alias → teamnamn)
  SUPABASE_URL             — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY     — service_role-nyckeln (får BARA ligga som GitHub-secret)
"""

import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

TZ = ZoneInfo("Europe/Stockholm")
DOC_KEY = "sabba:chatters:v1"
MAX_SALES_KEPT = 500

# Exempel på meddelanden som parsas (sales OCH tips räknas):
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


def week_id(dt):
    local = dt.astimezone(TZ)
    y, w, _ = local.isocalendar()
    return f"{y}-W{w:02d}"


class Supa:
    """Läser/skriver raden (key, value, updated_at) i tabellen sabba_data via REST."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/sabba_data"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def load(self):
        r = requests.get(self.base, headers=self.headers,
                         params={"key": f"eq.{DOC_KEY}", "select": "value"}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        data = {"v": 1, "updatedAt": 0, "weeks": {}, "sales": [], "last": {}}
        if rows:
            try:
                d = json.loads(rows[0].get("value") or "{}")
                if isinstance(d, dict):
                    d.setdefault("weeks", {})
                    d.setdefault("sales", [])
                    d.setdefault("last", {})
                    data = d
            except Exception:
                pass
        return data

    def save(self, data):
        r = requests.post(
            self.base,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
            json=[{"key": DOC_KEY, "value": json.dumps(data, ensure_ascii=False),
                   "updated_at": data["updatedAt"]}],
            timeout=30,
        )
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

    data = supa.load()
    # Dubblettskydd v2: boten kan posta SAMMA sale i flera chattar (stora kanalen +
    # modellens egen grupp). Utöver meddelande-id:t dedupas därför på ett innehålls-
    # fingeravtryck (minut + belopp + chattare + modell). Första körningen efter
    # uppgraderingen byggs allt om från grunden så gamla dubbletter försvinner.
    # dv 4 = kundnamn (Buyer) sparas — allt byggs om från grunden så historiken får med kunderna.
    if data.get("dv") != 4:
        data = {"v": 1, "dv": 4, "updatedAt": 0, "weeks": {}, "sales": [], "last": {}}
    seen = {s.get("id") for s in data["sales"]}
    fps = set()
    for s in data["sales"]:
        fps.add(f"{int(s.get('t', 0)) // 60}|{s.get('amount')}|{str(s.get('chatter', '')).lower()}|{str(s.get('creator', '')).lower()}")
    added = 0
    skipped_dupes = 0
    errors = []

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
            last = int(data["last"].get(str(dialog.id), 0))
            top = last
            limit = 3000 if last == 0 else 400
            try:
                msgs = []
                for term in ("New Sale", "New Tip"):
                    msgs.extend(client.iter_messages(dialog.id, limit=limit,
                                                     min_id=last, search=term))
                for msg in msgs:
                    if msg.id > top:
                        top = msg.id
                    if bot_id is not None and msg.sender_id != bot_id:
                        continue
                    sale = parse_sale(msg.raw_text)
                    if not sale:
                        continue
                    sid = f"{dialog.id}:{msg.id}"
                    if sid in seen:
                        continue
                    alias = sale["sent_by"]
                    name = cmap.get(alias.lower(), alias) or "Okänd"
                    # Samma sale postad i en annan chatt? Hoppa — den är redan räknad.
                    # Kollar minuten samt minuterna intill, ifall kopiorna postades
                    # några sekunder isär över en minutgräns.
                    mi = int(msg.date.timestamp()) // 60
                    tail = f"|{sale['amount']}|{name.lower()}|{sale['creator'].lower()}"
                    if any(f"{m}{tail}" in fps for m in (mi - 1, mi, mi + 1)):
                        skipped_dupes += 1
                        continue
                    seen.add(sid)
                    fps.add(f"{mi}{tail}")
                    wid = week_id(msg.date)
                    w = data["weeks"].setdefault(wid, {})
                    c = w.setdefault(name, {"count": 0, "total": 0.0})
                    c["count"] += 1
                    c["total"] = round(c["total"] + sale["amount"], 2)
                    data["sales"].append({
                        "id": sid,
                        "t": int(msg.date.timestamp()),
                        "amount": sale["amount"],
                        "chatter": name,
                        "creator": sale["creator"],
                        "site": sale["site"],
                        "buyer": sale["buyer"],
                    })
                    added += 1
                if top > last:
                    data["last"][str(dialog.id)] = top
            except Exception as e:
                errors.append(f"{dialog.name}: {e}")

    data["sales"].sort(key=lambda s: s["t"])
    if len(data["sales"]) > MAX_SALES_KEPT:
        data["sales"] = data["sales"][-MAX_SALES_KEPT:]
    data["updatedAt"] = int(time.time() * 1000)
    supa.save(data)
    print(f"Klart: {added} nya sales, {len(data['weeks'])} veckor, {len(data['sales'])} sparade, "
          f"{skipped_dupes} dubbletter bortfiltrerade.")
    for e in errors:
        print("varning:", e)
    # Fel i enskilda chattar fäller inte körningen — men noll åtkomst alls ska synas rött.
    if bot_id is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
