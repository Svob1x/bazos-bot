#!/usr/bin/env python3
"""
Bazos bot - hlídá nové inzeráty na bazos.cz (alu kola R16 5x112).

Použití:
    pip install requests beautifulsoup4
    python bazos_bot.py            # jednorázová kontrola (vhodné pro cron)
    python bazos_bot.py --loop     # běží pořád, kontroluje každých N minut

Notifikace (vše volitelné, zapne se jen to, co má vyplněné proměnné prostředí):
    E-mail:   SMTP_USER, SMTP_PASS, EMAIL_TO  (+ SMTP_HOST, SMTP_PORT, výchozí Gmail)
    SMS:      TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, SMS_TO  (číslo ve tvaru +420...)
    Telegram: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

# ===================== NASTAVENÍ =====================
HLEDANY_TEXT = "alu kola 5x112 r16"   # co se zadá do vyhledávání Bazoše
CENA_OD = ""                          # např. "2000", prázdné = bez omezení
CENA_DO = "12000"                     # max. cena v Kč, prázdné = bez omezení
PSC = ""                              # např. "11000" pro hledání v okolí
OKOLI_KM = "25"                       # okruh kolem PSČ
POCET_STRAN = 2                       # kolik stránek výsledků projít (40 inzerátů / strana)
INTERVAL_MIN = 15                     # interval pro režim --loop

# Kontrola, že inzerát opravdu odpovídá (hledá se v nadpisu + popisku)
VZOR_ROZTEC = re.compile(r"5\s*[x×/]\s*112", re.I)
VZOR_PRUMER = re.compile(r"(r\s?16\b|\b16\s*(\"|''|”|palc|col|inch)|\b16\s*x\s*\d)", re.I)

SOUBOR_VIDENYCH = Path(__file__).with_name("bazos_videne.json")
# =====================================================

ZAKLAD = "https://auto.bazos.cz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}


def url_stranky(strana: int) -> str:
    # Bazoš: 1. strana = "/", další strany "/40/", "/80/"... (40 inzerátů na stranu)
    offset = "" if strana == 0 else f"{strana * 40}/"
    params = (
        f"hledat={quote_plus(HLEDANY_TEXT)}&rubriky=auto"
        f"&hlokalita={PSC}&humkreis={OKOLI_KM}"
        f"&cenaod={CENA_OD}&cenado={CENA_DO}&Submit=Hledat&order=&crp=&kitx=ano"
    )
    return f"{ZAKLAD}/{offset}?{params}"


def stahni_inzeraty() -> list[dict]:
    inzeraty = {}
    for strana in range(POCET_STRAN):
        url = url_stranky(strana)
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        bloky = soup.select("div.inzeraty")
        if not bloky:
            break
        for blok in bloky:
            odkaz = blok.select_one("h2.nadpis a") or blok.find("a", href=re.compile(r"/inzerat/\d+"))
            if not odkaz:
                continue
            m = re.search(r"/inzerat/(\d+)", odkaz["href"])
            if not m:
                continue
            id_ = m.group(1)
            cena = blok.select_one(".inzeratycena")
            popis = blok.select_one(".popis")
            lokalita = blok.select_one(".inzeratylok")
            inzeraty[id_] = {
                "id": id_,
                "nadpis": odkaz.get_text(strip=True),
                "url": urljoin(ZAKLAD, odkaz["href"]),
                "cena": cena.get_text(" ", strip=True) if cena else "?",
                "popis": popis.get_text(" ", strip=True) if popis else "",
                "lokalita": lokalita.get_text(" ", strip=True) if lokalita else "",
            }
        time.sleep(1.5)  # šetrně k serveru
    return list(inzeraty.values())


def cena_cislo(cena: str):
    """'8 500 Kč' -> 8500; 'Dohodou' / 'V textu' -> None."""
    cislice = re.sub(r"[^\d]", "", cena)
    return int(cislice) if cislice else None


def odpovida(inz: dict) -> bool:
    text = f"{inz['nadpis']} {inz['popis']}"
    if not (VZOR_ROZTEC.search(text) and VZOR_PRUMER.search(text)):
        return False
    if CENA_DO:
        c = cena_cislo(inz["cena"])
        if c is not None and c > int(CENA_DO):
            return False   # inzeráty bez ceny ("Dohodou") se pouští dál
    return True


def nacti_videne() -> set[str]:
    if SOUBOR_VIDENYCH.exists():
        return set(json.loads(SOUBOR_VIDENYCH.read_text(encoding="utf-8")))
    return set()


def uloz_videne(videne: set[str]) -> None:
    SOUBOR_VIDENYCH.write_text(json.dumps(sorted(videne)), encoding="utf-8")


def posli_telegram(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": False},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Telegram chyba: {e}", file=sys.stderr)


def posli_email(predmet: str, text: str) -> None:
    """E-mail přes SMTP (např. Gmail s heslem aplikace)."""
    uzivatel, heslo, komu = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (uzivatel and heslo and komu):
        return
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = predmet
    msg["From"] = uzivatel
    msg["To"] = komu
    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"),
                              int(os.getenv("SMTP_PORT", "465")), timeout=20) as s:
            s.login(uzivatel, heslo)
            s.sendmail(uzivatel, [a.strip() for a in komu.split(",")], msg.as_string())
    except Exception as e:
        print(f"E-mail chyba: {e}", file=sys.stderr)


def posli_sms(text: str) -> None:
    """SMS přes Twilio (placená služba, pár korun za SMS)."""
    sid, token = os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN")
    od, komu = os.getenv("TWILIO_FROM"), os.getenv("SMS_TO")
    if not (sid and token and od and komu):
        return
    try:
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": od, "To": komu, "Body": text[:600]},
            auth=(sid, token), timeout=15,
        )
    except requests.RequestException as e:
        print(f"SMS chyba: {e}", file=sys.stderr)


def kontrola() -> bool:
    """Vrací False, když se nepodařilo stáhnout stránku."""
    videne = nacti_videne()
    prvni_beh = not videne
    try:
        inzeraty = stahni_inzeraty()
    except requests.RequestException as e:
        print(f"[{time.strftime('%H:%M')}] Chyba stahování: {e}", file=sys.stderr)
        return False

    nove = [i for i in inzeraty if i["id"] not in videne and odpovida(i)]
    videne.update(i["id"] for i in inzeraty)
    uloz_videne(videne)

    if prvni_beh:
        print(f"První spuštění: uloženo {len(inzeraty)} stávajících inzerátů, "
              f"příště hlásím jen nové.")
        return True

    if not nove:
        print(f"[{time.strftime('%d.%m. %H:%M')}] Nic nového.")
        return True

    for i in nove:
        zprava = (f"🛞 Nový inzerát: {i['nadpis']}\n"
                  f"💰 {i['cena']}   📍 {i['lokalita']}\n{i['url']}")
        print(zprava + "\n")
        posli_telegram(zprava)
        posli_email(f"Bazoš: {i['nadpis']} ({i['cena']})", zprava)
        posli_sms(f"Bazos: {i['nadpis']} {i['cena']} {i['url']}")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Hlídač inzerátů na bazos.cz")
    p.add_argument("--loop", action="store_true", help="běžet pořád dokola")
    args = p.parse_args()

    if not args.loop:
        sys.exit(0 if kontrola() else 1)
    while True:
        try:
            kontrola()
        except Exception as e:  # ať bot nespadne kvůli jedné chybě
            print(f"Neočekávaná chyba: {e}", file=sys.stderr)
        time.sleep(INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
