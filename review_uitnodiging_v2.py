#!/usr/bin/env python3
"""
HollandLamp — Review Uitnodigingen (via Cloudflare Worker)
==========================================================
Versie : 2.0

Dit script bevat GEEN gevoelige gegevens.
Alle secrets (Magento token, mail wachtwoord) staan veilig
in de Cloudflare Worker als encrypted secrets.

Dit script doet alleen:
  1. Worker vragen om orders op te halen
  2. Bijhouden welke orders al uitgenodigd zijn (lokale DB)
  3. Worker vragen om mails te versturen

Gebruik:
  python3 review_uitnodiging.py              # normale run
  python3 review_uitnodiging.py --test       # logt maar stuurt niet
  python3 review_uitnodiging.py --droogloop  # toont orders, raakt niets aan
  python3 review_uitnodiging.py --afmelden info@klant.nl
"""

import sqlite3
import requests
import argparse
from datetime import datetime, timedelta

# ══════════════════════════════════════════════════════════════
# CONFIGURATIE — alleen niet-gevoelige instellingen hier
# ══════════════════════════════════════════════════════════════

# URL van jullie Cloudflare Worker
WORKER_URL    = "https://throbbing-bread-6b8b.marketinghollandlamp.workers.dev"

# Worker secret — dit is het enige "wachtwoord" in dit script.
# Het is geen Magento- of mailwachtwoord, alleen een sleutel
# zodat de Worker weet dat het verzoek van jullie script komt.
# Stel zelf in: bijv. een willekeurige string van 32 tekens.
WORKER_SECRET = "HL-reviews-2026-XkQ9"

DELAY_DAGEN       = 7    # Wacht X dagen na 'complete' voor eerste mail
HERINNERING_DAGEN = 5    # Stuur herinnering X dagen ná eerste mail
DB_PATH           = "review_log.db"

# ══════════════════════════════════════════════════════════════
# DATABASE (lokaal — houdt bij wat al verstuurd is)
# ══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS uitnodigingen (
            order_id        TEXT PRIMARY KEY,
            email           TEXT NOT NULL,
            voornaam        TEXT,
            verstuurd_op    TEXT,
            herinnering_op  TEXT,
            status          TEXT DEFAULT 'uitgenodigd'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS afmeldingen (
            email       TEXT PRIMARY KEY,
            afgemeld_op TEXT NOT NULL,
            order_id    TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_al_uitgenodigd(order_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, verstuurd_op FROM uitnodigingen WHERE order_id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row


def is_afgemeld(email):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM afmeldingen WHERE email = ?", (email.lower(),))
    row = c.fetchone()
    conn.close()
    return row is not None


def registreer_afmelding(email, order_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO afmeldingen (email, afgemeld_op, order_id)
        VALUES (?, ?, ?)
    """, (email.lower(), datetime.now().isoformat(), order_id))
    c.execute("""
        UPDATE uitnodigingen SET status = 'uitgeschreven' WHERE email = ?
    """, (email.lower(),))
    conn.commit()
    conn.close()
    print(f"  ✓ Afmelding verwerkt voor {email}")


def sla_uitnodiging_op(order_id, email, voornaam, status="uitgenodigd"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    nu = datetime.now().isoformat()
    if status == "uitgenodigd":
        c.execute("""
            INSERT OR IGNORE INTO uitnodigingen
            (order_id, email, voornaam, verstuurd_op, status)
            VALUES (?, ?, ?, ?, ?)
        """, (order_id, email.lower(), voornaam, nu, status))
    elif status == "herinnerd":
        c.execute("""
            UPDATE uitnodigingen
            SET herinnering_op = ?, status = 'herinnerd'
            WHERE order_id = ?
        """, (nu, order_id))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
# WORKER COMMUNICATIE
# ══════════════════════════════════════════════════════════════

def worker_headers():
    return {
        "Content-Type":    "application/json",
        "X-Worker-Secret": WORKER_SECRET,
    }


def haal_orders_op():
    """Vraag de Worker om orders op te halen uit Magento."""
    nu          = datetime.now()
    grens_nieuw = (nu - timedelta(days=DELAY_DAGEN)).strftime("%Y-%m-%d %H:%M:%S")
    grens_oud   = (nu - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        resp = requests.post(
            f"{WORKER_URL}/orders",
            headers=worker_headers(),
            json={"grens_nieuw": grens_nieuw, "grens_oud": grens_oud},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("orders", [])
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Worker fout bij ophalen orders: {e}")
        return []


def verstuur_via_worker(email, voornaam, order_id,
                        is_herinnering=False, testmodus=False):
    """Vraag de Worker om een mail te versturen."""
    label = "[HERINNERING] " if is_herinnering else ""

    if testmodus:
        print(f"  [TESTMODUS] {label}Zou mail sturen naar {email} (order {order_id})")
        return True

    try:
        resp = requests.post(
            f"{WORKER_URL}/send-mail",
            headers=worker_headers(),
            json={
                "naar_email":    email,
                "voornaam":      voornaam,
                "order_id":      order_id,
                "is_herinnering": is_herinnering,
            },
            timeout=30
        )
        resp.raise_for_status()
        print(f"  ✓ {label}Mail verstuurd → {email} (order {order_id})")
        return True
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Worker fout bij versturen mail: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# HOOFD-LOGICA
# ══════════════════════════════════════════════════════════════

def verwerk_uitnodigingen(testmodus=False, droogloop=False):
    orders = haal_orders_op()
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] {len(orders)} geschikte orders gevonden")
    print("-" * 50)

    tellers = {"nieuw": 0, "herinnering": 0, "overgeslagen": 0, "fout": 0}

    for order in orders:
        order_id = str(order.get("order_id", "?"))
        email    = (order.get("email") or "").strip()
        voornaam = (order.get("voornaam") or "klant").strip()

        if not email or "@" not in email:
            tellers["overgeslagen"] += 1
            continue

        if is_afgemeld(email):
            print(f"  ⊘ Overgeslagen (afgemeld): {email}")
            tellers["overgeslagen"] += 1
            continue

        record = is_al_uitgenodigd(order_id)

        if record is None:
            # ── Eerste uitnodiging ──
            if droogloop:
                print(f"  [DROOGLOOP] Zou uitnodigen: {email} (order {order_id})")
                tellers["nieuw"] += 1
                continue

            ok = verstuur_via_worker(email, voornaam, order_id,
                                     is_herinnering=False, testmodus=testmodus)
            if ok:
                sla_uitnodiging_op(order_id, email, voornaam, "uitgenodigd")
                tellers["nieuw"] += 1
            else:
                tellers["fout"] += 1

        elif record[0] == "uitgenodigd":
            # ── Herinnering nodig? ──
            verstuurd_op = datetime.fromisoformat(record[1])
            wacht_tot    = verstuurd_op + timedelta(days=HERINNERING_DAGEN)

            if datetime.now() >= wacht_tot:
                if droogloop:
                    print(f"  [DROOGLOOP] Zou herinnering sturen: {email} (order {order_id})")
                    tellers["herinnering"] += 1
                    continue

                ok = verstuur_via_worker(email, voornaam, order_id,
                                         is_herinnering=True, testmodus=testmodus)
                if ok:
                    sla_uitnodiging_op(order_id, email, voornaam, "herinnerd")
                    tellers["herinnering"] += 1
                else:
                    tellers["fout"] += 1
            else:
                tellers["overgeslagen"] += 1
        else:
            tellers["overgeslagen"] += 1

    print("-" * 50)
    print(f"  Nieuw uitgenodigd : {tellers['nieuw']}")
    print(f"  Herinneringen     : {tellers['herinnering']}")
    print(f"  Overgeslagen      : {tellers['overgeslagen']}")
    print(f"  Fouten            : {tellers['fout']}")


def main():
    parser = argparse.ArgumentParser(description="HollandLamp Review Uitnodigingen")
    parser.add_argument("--test",      action="store_true",
                        help="Logt mails maar verstuurt ze niet")
    parser.add_argument("--droogloop", action="store_true",
                        help="Toont wat er zou gebeuren, raakt niets aan")
    parser.add_argument("--afmelden",  metavar="EMAIL",
                        help="Schrijf een e-mailadres handmatig uit")
    args = parser.parse_args()

    print("═" * 50)
    print("  HollandLamp — Review Uitnodigingen v2.0")
    print("═" * 50)

    init_db()

    if args.afmelden:
        registreer_afmelding(args.afmelden)
        return

    verwerk_uitnodigingen(testmodus=args.test, droogloop=args.droogloop)
    print("\n✓ Klaar\n")


if __name__ == "__main__":
    main()
