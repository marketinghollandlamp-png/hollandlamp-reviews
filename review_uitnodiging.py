#!/usr/bin/env python3
"""
HollandLamp — Automatische Review Uitnodigingen
================================================
Versie : 4.0
Draait : via GitHub Actions elke dag om 09:00

Werking:
  1. Haalt orders op via Cloudflare Worker (die praat met Magento)
  2. Filtert op: bedrag, verzenddag, nog niet uitgenodigd
  3. Verstuurt mail via Gmail SMTP met links naar Trustpilot + Google
  4. Slaat alles op in SQLite database
  5. Slaat alles op in SQLite database

Modus via omgevingsvariabele MODUS:
  normaal   → volledig automatisch
  droogloop → toont wat er zou gebeuren, verstuurt niets
  test      → verstuurt alleen naar het testadres
"""

import os
import sys
import sqlite3
import smtplib
import requests
import json
import argparse
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

# ══════════════════════════════════════════════════════════════
# CONFIGURATIE — via GitHub Secrets (omgevingsvariabelen)
# ══════════════════════════════════════════════════════════════

WORKER_URL    = os.getenv("WORKER_URL",    "https://plain-shape-a0a7.marketinghollandlamp.workers.dev")
WORKER_SECRET = os.getenv("WORKER_SECRET", "HL-reviews-2026-XkQ9")
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "marketinghollandlamp@gmail.com")
SMTP_PASS     = os.getenv("SMTP_PASS",     "")          # Ingesteld als GitHub Secret
AFZENDER_NAAM = "HollandLamp"
AFZENDER_MAIL = "info@hollandlamp.nl"                   # Verzenden als alias in Gmail
TEST_MAIL     = os.getenv("TEST_MAIL",     SMTP_USER)   # Testmails gaan hierheen

# Review links
KIYOH_URL      = "https://www.kiyoh.com/reviews/1045792/hollandlamp"
GOOGLE_URL     = "https://search.google.com/local/writereview?placeid=ChIJBXsLUDFUz0cRHVuK3AHyRRg"
TRUSTPILOT_URL = "https://www.trustpilot.com/review/hollandlamp.nl"
AFMELD_URL     = f"{WORKER_URL}/afmelden"

# Instellingen
DELAY_DAGEN       = int(os.getenv("DELAY_DAGEN",    "7"))   # Dagen na bestelling
MIN_BEDRAG        = float(os.getenv("MIN_BEDRAG",  "50"))   # Minimum orderbedrag
MAX_ORDERS        = int(os.getenv("MAX_ORDERS",   "100"))   # Max orders per run
DB_PATH           = "review_log.db"
MODUS             = os.getenv("MODUS", "normaal")           # normaal/droogloop/test

# ── Klantgroep filter ─────────────────────────────────────────
# customer_group_id uit Magento: 0=gast, 1=particulier, 2=wholesale
# Voeg hier extra groepen toe zodra je de IDs van Jeroen hebt.
KLANTGROEP_CONFIG = {
    0: {"actief": True,  "min_bedrag": 50.0},   # Gast
    1: {"actief": True,  "min_bedrag": 50.0},   # Particulier
    2: {"actief": True,  "min_bedrag": 200.0},  # Wholesale / zakelijk
}
KLANTGROEP_DEFAULT = {"actief": True, "min_bedrag": 50.0}   # Onbekende groepen

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════

class Logger:
    def __init__(self):
        self.regels = []
        self.tellers = {"nieuw": 0, "herinnering": 0, "overgeslagen": 0, "fout": 0}

    def log(self, msg, niveau="info"):
        prefix = {"info": "  ", "ok": "  ✓", "fout": "  ✗", "skip": "  ⊘", "warn": "  ⚠"}
        regel = f"{prefix.get(niveau,'  ')} {msg}"
        print(regel)
        self.regels.append(regel)

    def samenvatting(self):
        lines = [
            "| Categorie | Aantal |",
            "|-----------|--------|",
            f"| Nieuw uitgenodigd | {self.tellers['nieuw']} |",
            f"| Herinneringen | {self.tellers['herinnering']} |",
            f"| Overgeslagen | {self.tellers['overgeslagen']} |",
            f"| Fouten | {self.tellers['fout']} |",
        ]
        try:
            with open("run_summary.txt", "w") as f:
                f.write("\n".join(lines))
        except Exception:
            pass

log = Logger()

# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS uitnodigingen (
            order_id        TEXT PRIMARY KEY,
            email           TEXT NOT NULL,
            voornaam        TEXT,
            bedrag          REAL,
            verstuurd_op    TEXT,
            herinnering_op  TEXT,
            status          TEXT DEFAULT 'uitgenodigd',
            modus           TEXT DEFAULT 'normaal'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS afmeldingen (
            email       TEXT PRIMARY KEY,
            afgemeld_op TEXT NOT NULL,
            order_id    TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            gestart_op  TEXT NOT NULL,
            modus       TEXT,
            nieuw       INTEGER DEFAULT 0,
            herinnering INTEGER DEFAULT 0,
            overgeslagen INTEGER DEFAULT 0,
            fouten      INTEGER DEFAULT 0,
            duur_sec    REAL
        )
    """)

    conn.commit()
    conn.close()


def is_uitgenodigd(order_id):
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


def sla_op(order_id, email, voornaam, bedrag, status="uitgenodigd"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    nu = datetime.now().isoformat()

    if status == "uitgenodigd":
        c.execute("""
            INSERT OR IGNORE INTO uitnodigingen
            (order_id, email, voornaam, bedrag, verstuurd_op, status, modus)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (order_id, email.lower(), voornaam, bedrag, nu, status, MODUS))
    elif status == "herinnerd":
        c.execute("""
            UPDATE uitnodigingen
            SET herinnering_op = ?, status = 'herinnerd'
            WHERE order_id = ?
        """, (nu, order_id))

    conn.commit()
    conn.close()


def sla_run_op(gestart, duur):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO runs (gestart_op, modus, nieuw, herinnering, overgeslagen, fouten, duur_sec)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        gestart.isoformat(), MODUS,
        log.tellers["nieuw"], log.tellers["herinnering"],
        log.tellers["overgeslagen"], log.tellers["fout"],
        round(duur, 2)
    ))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
# WORKER / MAGENTO
# ══════════════════════════════════════════════════════════════

def haal_orders_op():
    nu          = datetime.now()
    grens_nieuw = (nu - timedelta(days=DELAY_DAGEN)).strftime("%Y-%m-%d %H:%M:%S")
    grens_oud   = (nu - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        resp = requests.post(
            f"{WORKER_URL}/orders",
            headers={
                "Content-Type":    "application/json",
                "X-Worker-Secret": WORKER_SECRET,
            },
            json={"grens_nieuw": grens_nieuw, "grens_oud": grens_oud},
            timeout=30
        )
        resp.raise_for_status()
        data   = resp.json()
        orders = data.get("orders", [])
        log.log(f"{len(orders)} orders opgehaald uit Magento (van {grens_nieuw[:10]} tot {grens_oud[:10]})")
        return orders

    except requests.exceptions.ConnectionError:
        log.log("Worker niet bereikbaar — check de Worker URL", "fout")
        return []
    except requests.exceptions.HTTPError as e:
        log.log(f"Worker HTTP fout: {e.response.status_code} — {e.response.text[:200]}", "fout")
        return []
    except Exception as e:
        log.log(f"Onverwachte fout bij ophalen orders: {e}", "fout")
        return []


# ══════════════════════════════════════════════════════════════
# E-MAIL
# ══════════════════════════════════════════════════════════════

def maak_html_mail(voornaam, order_id, is_herinnering=False):
    afmeld_url = f"{AFMELD_URL}?token={requests.utils.quote(f'{order_id}:{AFZENDER_MAIL}')}"

    intro = (
        "We hebben u eerder uitgenodigd om een review achter te laten voor uw bestelling bij HollandLamp. "
        "Mocht u hier nog even tijd voor hebben, stellen we dat zeer op prijs."
    ) if is_herinnering else (
        "We hopen dat u tevreden bent met uw aankoop en de bezorging. "
        "Heeft u een momentje om een korte, eerlijke review achter te laten?"
    )

    herinnering_badge = """
  <tr>
    <td style="background:#FEF3E8;padding:10px 28px;border-bottom:2px solid #F8C99A">
      <p style="margin:0;font-size:12px;color:#D4580A;font-weight:700;text-align:center;letter-spacing:0.5px">
        &#9679; HERINNERING — Uw review staat nog open
      </p>
    </td>
  </tr>""" if is_herinnering else ""

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
</head>
<body style="margin:0;padding:0;background:#F8F8F8;font-family:Arial,sans-serif">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
  style="background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.10);max-width:600px">

  <!-- Header -->
  <tr>
    <td style="background:#D4580A;padding:24px 32px">
      <img src="https://raw.githubusercontent.com/marketinghollandlamp-png/hollandlamp-reviews/main/logo.png"
           alt="HollandLamp" style="max-height:36px;display:block">
    </td>
  </tr>

  <!-- Herinnering badge (alleen bij herinnering) -->
  {herinnering_badge}

  <!-- Oranje accentlijn -->
  <tr>
    <td style="background:#D4580A;padding:0;height:4px;font-size:0;line-height:0">&nbsp;</td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:32px 32px 24px;color:#333333;font-size:15px;line-height:1.8">
      <p style="margin:0 0 18px 0;font-size:16px;color:#0D1117;font-weight:700">Beste {voornaam},</p>
      <p style="margin:0 0 16px 0;color:#333333">
        Hartelijk dank voor uw bestelling bij HollandLamp
        <span style="color:#666666;font-size:13px">&nbsp;(order #{order_id})</span>.
      </p>
      <p style="margin:0 0 16px 0;color:#333333">{intro}</p>
      <p style="margin:0 0 8px 0;color:#333333">
        Uw mening helpt andere klanten bij hun keuze én helpt ons onze service te verbeteren.
        Alle reviews zijn welkom — ook als u iets verbeterd had willen zien.
      </p>
    </td>
  </tr>

  <!-- Knoppen -->
  <tr>
    <td style="padding:8px 32px 36px;text-align:center;background:#ffffff">
      <p style="margin:0 0 20px 0;font-size:14px;color:#666666">Kies uw favoriete platform:</p>
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto">
        <tr>
          <td style="padding:6px">
            <a href="{GOOGLE_URL}"
               style="display:inline-block;background:#FBBC04;color:#0D1117;font-family:Arial,sans-serif;font-weight:700;font-size:14px;padding:14px 24px;border-radius:6px;text-decoration:none;letter-spacing:0.3px">
              &#9679;&nbsp; Google Reviews
            </a>
          </td>
          <td style="padding:6px">
            <a href="{TRUSTPILOT_URL}"
               style="display:inline-block;background:#00B67A;color:#ffffff;font-family:Arial,sans-serif;font-weight:700;font-size:14px;padding:14px 24px;border-radius:6px;text-decoration:none;letter-spacing:0.3px">
              &#10003;&nbsp; Trustpilot
            </a>
          </td>
        </tr>
      </table>
      <p style="margin:18px 0 0;font-size:12px;color:#999999">
        Het schrijven van een review duurt maar 2 minuten.
      </p>
    </td>
  </tr>

  <!-- Handtekening -->
  <tr>
    <td style="padding:20px 32px 24px;border-top:2px solid #F8C99A;background:#FEF3E8">
      <p style="margin:0;color:#0D1117;font-size:14px;line-height:1.7">
        Met vriendelijke groet,<br>
        <strong style="color:#D4580A">Team HollandLamp</strong><br>
        <span style="color:#666666;font-size:13px">
          &#128222; 072 – 26 000 00 &nbsp;|&nbsp;
          <a href="mailto:info@hollandlamp.nl" style="color:#D4580A;text-decoration:none">info@hollandlamp.nl</a>
        </span>
      </p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#F0F0F0;padding:14px 32px;border-top:1px solid #E0E0E0">
      <p style="margin:0;font-size:11px;color:#999999;line-height:1.6;text-align:center">
        U ontvangt dit bericht omdat u een aankoop heeft gedaan bij
        <a href="https://www.hollandlamp.nl" style="color:#D4580A;text-decoration:none">HollandLamp.nl</a>.<br>
        Wilt u geen review-uitnodigingen meer ontvangen?
        <a href="{afmeld_url}" style="color:#999999;text-decoration:underline">Klik hier om u af te melden</a>.
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


def verstuur_mail(naar_email, voornaam, order_id,
                  is_herinnering=False):

    # In testmodus sturen we naar het testadres
    echte_ontvanger = TEST_MAIL if MODUS == "test" else naar_email

    onderwerp = (
        "Nog even: wat vond u van uw bestelling bij HollandLamp?"
        if is_herinnering else
        "Wat vond u van uw bestelling bij HollandLamp?"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"]         = onderwerp
    msg["From"]            = formataddr((AFZENDER_NAAM, AFZENDER_MAIL))
    msg["To"]              = echte_ontvanger
    msg["Reply-To"]        = AFZENDER_MAIL
    msg["List-Unsubscribe"] = f"<{AFMELD_URL}?token={order_id}:{naar_email}>"

    html = maak_html_mail(voornaam, order_id, is_herinnering)
    msg.attach(MIMEText(html, "html", "utf-8"))

    if not SMTP_PASS:
        log.log(f"SMTP wachtwoord niet ingesteld — mail gesimuleerd voor {naar_email}", "warn")
        return True

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, echte_ontvanger, msg.as_string())

        label = "[HERINNERING] " if is_herinnering else ""
        test_label = f" → {TEST_MAIL} (test)" if MODUS == "test" else ""
        log.log(f"{label}Mail verstuurd → {naar_email}{test_label}", "ok")
        return True

    except smtplib.SMTPAuthenticationError:
        log.log(f"SMTP authenticatie mislukt — check het app-wachtwoord", "fout")
        return False
    except smtplib.SMTPException as e:
        log.log(f"SMTP fout voor {naar_email}: {e}", "fout")
        return False
    except Exception as e:
        log.log(f"Onverwachte mailfout voor {naar_email}: {e}", "fout")
        return False


# ══════════════════════════════════════════════════════════════
# HOOFD LOGICA
# ══════════════════════════════════════════════════════════════

def verwerk():
    # ── Testmodus: stuur direct een testmail zonder orders op te halen ──
    if MODUS == "test":
        log.log("TESTMODUS — testmail wordt direct verstuurd zonder Worker/Magento")
        ok = verstuur_mail(TEST_MAIL, "Jan", "TEST-001", is_herinnering=False)
        if ok:
            log.tellers["nieuw"] += 1
            log.log(f"Testmail verstuurd naar {TEST_MAIL}", "ok")
        else:
            log.tellers["fout"] += 1
        return

    orders = haal_orders_op()

    if not orders:
        log.log("Geen orders gevonden of Worker niet bereikbaar", "warn")
        return

    log.log(f"{len(orders)} orders opgehaald, klantgroep-filter wordt toegepast")
    print("-" * 52)

    for order in orders:
        order_id   = str(order.get("order_id", "?"))
        email      = (order.get("email") or "").strip().lower()
        voornaam   = (order.get("voornaam") or "klant").strip()
        bedrag     = float(order.get("bedrag", 0))
        groep_id   = order.get("customer_group_id")
        groep_id   = int(groep_id) if groep_id is not None else None
        groep_cfg  = KLANTGROEP_CONFIG.get(groep_id, KLANTGROEP_DEFAULT)
        groep_naam = {0: "gast", 1: "particulier", 2: "wholesale"}.get(groep_id, f"groep {groep_id}")

        # Klantgroep uitgeschakeld?
        if not groep_cfg["actief"]:
            log.log(f"Overgeslagen ({groep_naam}): {email} — klantgroep niet actief", "skip")
            log.tellers["overgeslagen"] += 1
            continue

        # Minimumbedrag per klantgroep
        min_bedrag = groep_cfg["min_bedrag"]
        if bedrag < min_bedrag:
            log.log(f"Overgeslagen ({groep_naam}): {email} — €{bedrag:.2f} < €{min_bedrag:.0f}", "skip")
            log.tellers["overgeslagen"] += 1
            continue

        # Validatie
        if not email or "@" not in email:
            log.log(f"Ongeld e-mailadres voor order {order_id}", "skip")
            log.tellers["overgeslagen"] += 1
            continue

        # AVG check
        if is_afgemeld(email):
            log.log(f"Afgemeld: {email}", "skip")
            log.tellers["overgeslagen"] += 1
            continue

        record = is_uitgenodigd(order_id)

        if record is None:
            # ── Eerste uitnodiging ──
            if MODUS == "droogloop":
                log.log(f"[DROOGLOOP] Zou uitnodigen: {email} (order {order_id}, €{bedrag:.2f})")
                log.tellers["nieuw"] += 1
                continue

            ok = verstuur_mail(email, voornaam, order_id, is_herinnering=False)
            if ok:
                sla_op(order_id, email, voornaam, bedrag, "uitgenodigd")
                log.tellers["nieuw"] += 1
            else:
                log.tellers["fout"] += 1

        else:
            # Al uitgenodigd — overslaan
            log.tellers["overgeslagen"] += 1


# ══════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════

def main():
    gestart = datetime.now()

    print("=" * 52)
    print(f"  HollandLamp — Review Uitnodigingen v4.0")
    print(f"  {gestart.strftime('%d-%m-%Y %H:%M:%S')}  |  Modus: {MODUS.upper()}")
    print("=" * 52)

    if MODUS == "droogloop":
        print("  ⚠ DROOGLOOP — geen mails worden verstuurd\n")
    elif MODUS == "test":
        print(f"  ⚠ TESTMODUS — mails gaan naar {TEST_MAIL}\n")

    init_db()
    verwerk()

    duur = (datetime.now() - gestart).total_seconds()

    print("-" * 52)
    print(f"  Nieuw uitgenodigd  : {log.tellers['nieuw']}")
    print(f"  Herinneringen      : {log.tellers['herinnering']}")
    print(f"  Overgeslagen       : {log.tellers['overgeslagen']}")
    print(f"  Fouten             : {log.tellers['fout']}")
    print(f"  Duur               : {duur:.1f}s")
    print("=" * 52)

    sla_run_op(gestart, duur)
    log.samenvatting()

    # Exit met foutcode als er fouten waren
    if log.tellers["fout"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
