#!/usr/bin/env python3
"""Beobachtet die Echtzeit-Verspätung der Sylt-Autozüge und hängt sie an delay_log.csv.

Zweck: über mehrere Tage prüfen, ob die Verspätungsdaten für BEIDE Züge
(AS = rot / DB, AZS = blau / RDC) verlässlich kommen — bevor entschieden wird,
ob und wie die Verspätung in die App wandert. Läuft per GitHub Action mehrmals
täglich, nur Standardbibliothek (urllib), kein pip.

Quelle: die Verbindungssuche zwischen den beiden Autoverlade-Terminals — dieselbe
API, die die DB-App nutzt.
"""
import json
import gzip
import csv
import os
import datetime
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
API = "https://www.bahn.de/web/api/angebote/fahrplan"
NIEBUELL = "A=1@O=Niebüll Autoverladung@X=8835785@Y=54784561@U=80@L=8085311@"
WESTERLAND = "A=1@O=Westerland (Sylt) Autoverladung@X=8313638@Y=54904737@U=80@L=8030918@"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delay_log.csv")


def query(ab_halt, an_halt, when):
    body = {
        "abfahrtsHalt": ab_halt, "ankunftsHalt": an_halt,
        "anfrageZeitpunkt": when, "ankunftSuche": "ABFAHRT", "klasse": "KLASSE_2",
        "produktgattungen": ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS",
                             "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"],
        "reisende": [{"typ": "ERWACHSENER",
                      "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}],
                      "alter": [], "anzahl": 1}],
        "schnelleVerbindungen": False, "sitzplatzOnly": False,
        "bikeCarriage": False, "reservierungsKontingenteVorhanden": False,
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept-Encoding": "gzip",
        "Accept": "application/json", "Accept-Language": "de-DE,de;q=0.9",
        "Origin": "https://www.bahn.de",
        "Referer": "https://www.bahn.de/buchung/fahrplan/suche"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


def minutes(a, b):
    fa = datetime.datetime.fromisoformat(a)
    fb = datetime.datetime.fromisoformat(b)
    return round((fb - fa).total_seconds() / 60)


def collect(ab_halt, an_halt, label, stamp, rows):
    """Nächste Abfahrten einer Richtung (mehrere Zeitfenster) an `rows` anhängen."""
    now = datetime.datetime.now()
    seen = set()
    for h in range(0, 9, 2):        # jetzt + 2/4/6/8 h – erfasst die kommenden Züge dicht genug
        when = (now + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:00")
        try:
            data = query(ab_halt, an_halt, when)
        except Exception as e:
            print(f"[{label}] {when} Abruf fehlgeschlagen: {e}")
            continue
        for v in data.get("verbindungen", []):
            for a in v.get("verbindungsAbschnitte", []):
                vm = a.get("verkehrsmittel", {})
                kat = vm.get("kategorie", "")
                if kat not in ("AS", "AZS"):
                    continue
                ab = a.get("startHalt", {}).get("abfahrt", {})
                soll, echt = ab.get("sollzeit"), ab.get("echtzeit")
                key = (kat, soll)
                if key in seen:
                    continue
                seen.add(key)
                delay = minutes(soll, echt) if soll and echt else ""
                ausl = ""
                for hlt in a.get("halte", []):
                    for m in hlt.get("auslastungsmeldungen", []):
                        if m.get("klasse") == "KLASSE_2":
                            ausl = m.get("stufe")
                rows.append([stamp, label, "rot" if kat == "AS" else "blau", vm.get("name"),
                             soll[11:16] if soll else "", echt[11:16] if echt else "", delay, ausl])


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    collect(NIEBUELL, WESTERLAND, "Nach Sylt", stamp, rows)
    collect(WESTERLAND, NIEBUELL, "Nach Niebuell", stamp, rows)
    rot = sum(1 for r in rows if r[2] == "rot")
    blau = sum(1 for r in rows if r[2] == "blau")
    print(f"{stamp}: {len(rows)} Fahrten erfasst — rot {rot}, blau {blau}")

    neu = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if neu:
            w.writerow(["abfragezeit", "richtung", "farbe", "zug", "soll", "ist", "verspaetung_min", "auslastung"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
