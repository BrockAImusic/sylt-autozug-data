#!/usr/bin/env python3
"""
Automatischer Fahrplan-Updater für "Sylt Autozug".

Holt die offiziellen Fahrpläne live von den Betreiber-Webseiten (kein API nötig),
parst sie, validiert streng und schreibt Resources/timetable.json – aber NUR, wenn
das Ergebnis plausibel ist. Schlägt das Parsing/die Validierung fehl, bleibt die
vorhandene Datei unangetastet und das Skript endet mit Exit-Code 1 (Fail-Safe →
CI schlägt Alarm, statt fehlerhafte Daten auszuspielen).

Quellen:
  - DB Sylt Shuttle (rot): syltshuttle.de – Sommer- und Winter-Fahrplanseite
    (URLs werden von der Fahrplan-Übersicht automatisch entdeckt). Die HTML-Tabellen
    enthalten Verladeschluss + Ankunft und Wochentagsregeln als Text
    (z. B. "04:05 1) (nur Mo - Fr)", "17:25 (nur Sa + So)").
  - RDC Autozug Sylt (blau): autozug-sylt.de/de/fahrplan/ – zwei Richtungs-Blöcke.

Nur Standardbibliothek (urllib) – läuft in GitHub Actions ohne pip install.

Usage: python3 tools/update_timetable.py [--check]
  --check : nur prüfen/parsen und Diff anzeigen, nichts schreiben.
"""

import json
import os
import re
import ssl
import sys
import html as htmllib
import datetime
import subprocess
import urllib.request

UA = "Mozilla/5.0 (compatible; SyltAutozugBot/1.0; +https://github.com/)"

DB_INDEX = "https://www.syltshuttle.de/syltshuttle-de/fahrplan"
RDC_URL = "https://www.autozug-sylt.de/de/fahrplan/"

# Stabile, kuratierte Rahmendaten (ändern sich nur selten und werden bewusst
# nicht aus dem HTML geraten).
SEASONS = {
    "summer": {"label": "Sommer", "ranges": [{"from": "2026-03-28", "to": "2026-11-01"}]},
    "winter": {"label": "Winter", "ranges": [
        {"from": "2025-12-14", "to": "2026-03-27"},
        {"from": "2026-11-02", "to": "2026-12-12"},
    ]},
}
HOLIDAYS = ["2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-05-14",
            "2026-05-25", "2026-10-03", "2026-10-31", "2026-12-25", "2026-12-26",
            "2027-01-01"]
OPERATORS = {
    "db": {
        "name": "DB Sylt Shuttle", "shortName": "Roter Autozug", "color": "#EC0016",
        "arrivalExact": True,
        "booking": {"url": "https://ticket.syltshuttle.de", "label": "Beim roten DB Sylt Shuttle buchen", "affiliateParam": None, "priceFrom": "19,99 €"},
        "status": {"url": "https://www.syltshuttle.de/syltshuttle-de/fahrplan"},
    },
    "rdc": {
        "name": "RDC Autozug Sylt", "shortName": "Blauer Autozug", "color": "#0B63A6",
        "arrivalExact": False,
        "booking": {"url": "https://buchung.autozug-sylt.de/shop002/", "label": "Beim blauen RDC Autozug buchen", "affiliateParam": None, "priceFrom": "19,90 €"},
        "status": {"url": "https://www.autozug-sylt.de/de/aktuelles/"},
    },
}
FLAG_LABELS = {"noMoto": "Keine Motorradbeförderung"}
RDC_TRAVEL_MINUTES = 45

TIME_RE = re.compile(r"([0-2]?\d:[0-5]\d)")


def fetch(url):
    """Robuster HTTPS-Abruf: urllib, bei TLS-/Netzproblemen Fallback auf curl."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        out = subprocess.run(["curl", "-sSL", "-m", "25", "-A", UA, url],
                             capture_output=True)
        if out.returncode != 0 or not out.stdout:
            raise RuntimeError(f"Abruf fehlgeschlagen: {url} ({out.stderr[:200]!r})")
        return out.stdout.decode("utf-8", errors="ignore")


def strip_tags(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()


def add_minutes(hhmm, minutes):
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def norm(hhmm):
    h, m = hhmm.split(":")
    return f"{int(h):02d}:{m}"


# ---------------------------------------------------------------- DB -------

def discover_db_pages(index_html):
    """Findet die Sommer-/Winter-Fahrplan-Unterseiten aus der Übersicht."""
    pages = {}
    for href in re.findall(r'href="([^"]+)"', index_html):
        low = href.lower()
        if "sommerfahrplan" in low or "/sommer" in low:
            pages.setdefault("summer", _abs(href))
        if "winterfahrplan" in low or "winter" in low and "fahrplan" in low:
            pages.setdefault("winter", _abs(href))
    return pages


def _abs(href):
    if href.startswith("http"):
        return href
    return "https://www.syltshuttle.de" + href


def parse_db_page(page_html):
    """Liest beide Richtungstabellen einer DB-Saisonseite.

    Richtung wird aus der Überschrift direkt VOR der Tabelle bestimmt
    ("Niebüll – Westerland" vs. "Westerland – Niebüll"); als Rückfall die
    Tabellen-Reihenfolge (DB listet immer zuerst Niebüll→Westerland)."""
    out = {"toIsland": [], "toMainland": []}
    tables = [(m.start(), m.group(1)) for m in
              re.finditer(r'<table[^>]*>(.*?)</table>', page_html, re.S | re.I)]
    for idx, (pos, tab) in enumerate(tables):
        ctext = strip_tags(page_html[max(0, pos - 500):pos])
        ni, we = ctext.rfind("Niebüll"), ctext.rfind("Westerland")
        if ni >= 0 and we >= 0:
            direction = "toIsland" if ni < we else "toMainland"
        elif idx < 2:
            direction = ("toIsland", "toMainland")[idx]
        else:
            continue
        for row in re.findall(r'<tr>(.*?)</tr>', tab, re.S | re.I):
            cells = [strip_tags(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)]
            if len(cells) < 2:
                continue
            close_cell, arr_cell = cells[0], cells[1]
            tm = TIME_RE.search(close_cell)
            am = TIME_RE.search(arr_cell)
            if not tm or not am:
                continue
            days = "all"
            if "nur Mo" in close_cell or "Mo - Fr" in close_cell or "Mo–Fr" in close_cell:
                days = "weekday"
            elif "Sa + So" in close_cell or "Sa+So" in close_cell or "nur Sa" in close_cell:
                days = "weekend"
            # Fußnote 1 = keine Motorradbeförderung; kann kombiniert sein ("1,3)").
            flags = ["noMoto"] if re.search(r"(?<!\d)1\s*[,)]", close_cell) else []
            out[direction].append({
                "close": norm(tm.group(1)), "arr": norm(am.group(1)),
                "days": days, "flags": flags, "arrExact": True,
            })
    return out


def db_services(index_html):
    pages = discover_db_pages(index_html)
    if "summer" not in pages or "winter" not in pages:
        raise ValueError(f"DB-Saisonseiten nicht gefunden: {pages}")
    services = []
    for season, url in (("summer", pages["summer"]), ("winter", pages["winter"])):
        parsed = parse_db_page(fetch(url))
        for direction in ("toIsland", "toMainland"):
            for e in parsed[direction]:
                services.append({"op": "db", "dir": direction, "season": season, **e})
    return services


# --------------------------------------------------------------- RDC -------

def parse_rdc(page_html):
    """Zwei Richtungs-Blöcke; Zeiten in Dokument-Reihenfolge.

    Split an der Rückrichtungs-Überschrift = das <h3>, in dem "Westerland" VOR
    "Niebüll" steht (robust gegenüber SVG-Markup im Heading)."""
    split = None
    for m in re.finditer(r'<h3[^>]*>(.*?)</h3>', page_html, re.S | re.I):
        txt = strip_tags(m.group(1))
        if "Westerland" in txt and "Niebüll" in txt and txt.index("Westerland") < txt.index("Niebüll"):
            split = m.start()
            break
    if split is None:
        split = len(page_html) // 2
    times = [(m.start(), norm(m.group(1))) for m in TIME_RE.finditer(page_html)]
    to_island = [t for pos, t in times if pos < split]
    to_mainland = [t for pos, t in times if pos >= split]
    return to_island, to_mainland


def rdc_services(page_html):
    to_island, to_mainland = parse_rdc(page_html)
    season = "winter" if re.search(r"Winterfahrplan", page_html, re.I) else "summer"
    services = []
    for direction, closes in (("toIsland", to_island), ("toMainland", to_mainland)):
        for c in closes:
            services.append({"op": "rdc", "dir": direction, "season": season,
                             "days": "all", "close": c, "arr": add_minutes(c, RDC_TRAVEL_MINUTES),
                             "arrExact": False, "flags": []})
    return services


# --------------------------------------------------------- Validierung -----

def validate(services):
    errs = []
    if not services:
        return ["keine Services geparst"]
    counts = {}
    for s in services:
        counts[(s["op"], s["dir"], s["season"])] = counts.get((s["op"], s["dir"], s["season"]), 0) + 1
    # DB: Sommer und Winter, beide Richtungen erwartet.
    for key in [("db", "toIsland", "summer"), ("db", "toMainland", "summer"),
                ("db", "toIsland", "winter"), ("db", "toMainland", "winter")]:
        if counts.get(key, 0) < 10:
            errs.append(f"zu wenige DB-Fahrten {key}: {counts.get(key,0)}")
    # RDC: mindestens eine Saison, beide Richtungen.
    rdc_dirs = {(o, d, se): c for (o, d, se), c in counts.items() if o == "rdc"}
    if sum(1 for k in rdc_dirs if k[1] == "toIsland") == 0 or sum(1 for k in rdc_dirs if k[1] == "toMainland") == 0:
        errs.append("RDC: eine Richtung fehlt")
    for (o, d, se), c in rdc_dirs.items():
        if c < 8:
            errs.append(f"zu wenige RDC-Fahrten {(o,d,se)}: {c}")
    # Zeitformat + Sortierbarkeit
    for s in services:
        if not TIME_RE.fullmatch(s["close"]) or not TIME_RE.fullmatch(s["arr"]):
            errs.append(f"ungültige Zeit: {s}")
            break
    return errs


def build_doc(services, data_version):
    services = sorted(services, key=lambda s: (s["op"], s["dir"], s["season"], s["close"]))
    return {
        "schemaVersion": 1,
        "dataVersion": data_version,
        "generatedAt": data_version + "T06:00:00Z",
        "note": "Alle Angaben ohne Gewähr. Zeiten = Verladeschluss (Check-in). "
                "Unabhängige App, keine offizielle App von DB oder RDC.",
        "seasons": SEASONS,
        "holidays": HOLIDAYS,
        "operators": OPERATORS,
        "flagLabels": FLAG_LABELS,
        "services": services,
    }


def services_signature(doc):
    """Vergleichbarer Fingerabdruck – ignoriert dataVersion/generatedAt."""
    return json.dumps(doc.get("services", []), sort_keys=True, ensure_ascii=False)


def main():
    check_only = "--check" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "timetable.json")

    try:
        services = db_services(fetch(DB_INDEX)) + rdc_services(fetch(RDC_URL))
    except Exception as e:  # Netzwerk/Parsing – Fail-Safe
        print(f"FEHLER beim Abruf/Parsen: {e}", file=sys.stderr)
        return 1

    errs = validate(services)
    if errs:
        print("VALIDIERUNG FEHLGESCHLAGEN – bestehende Daten bleiben unverändert:", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        return 1

    today = datetime.date.today().isoformat()
    new_doc = build_doc(services, today)

    old_sig = None
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old_doc = json.load(f)
        old_sig = services_signature(old_doc)

    new_sig = services_signature(new_doc)
    changed = new_sig != old_sig
    print(f"Geparst: {len(services)} Fahrten. Änderung: {'JA' if changed else 'nein'}.")

    if check_only:
        if changed:
            print("(--check) Es gäbe eine Änderung.")
        return 0

    if not changed:
        print("Keine Änderung – Datei bleibt wie sie ist.")
        return 0

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(new_doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Aktualisiert: {out_path} (dataVersion {today})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
