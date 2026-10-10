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
# RDC-Saisons werden NICHT mehr fest verdrahtet, sondern aus der Zeile
# "GÜLTIG: 21. März bis 8. November 2026" jedes Fahrplan-PDFs gelesen (siehe
# rdc_services). RDC faehrt anders als die DB (Sommer eine Woche laenger), und
# sobald RDC den Winterfahrplan veroeffentlicht, landet er so automatisch als
# "winter" in den Daten – vorher wuerde er faelschlich als Sommer eingelesen.
HOLIDAYS = ["2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-05-14",
            "2026-05-25", "2026-10-03", "2026-10-31", "2026-12-25", "2026-12-26",
            "2027-01-01"]
OPERATORS = {
    "db": {
        "name": "DB Sylt Shuttle", "shortName": "Roter Autozug", "color": "#EC0016",
        "arrivalExact": True,
        "booking": {"url": "https://ticket.syltshuttle.de", "label": "Beim roten DB Sylt Shuttle buchen", "affiliateParam": None, "affiliateUrl": None, "priceFrom": "19,99 €"},
        "status": {"url": "https://www.syltshuttle.de/syltshuttle-de/fahrplan"},
    },
    "rdc": {
        "name": "RDC Autozug Sylt", "shortName": "Blauer Autozug", "color": "#0B63A6",
        "arrivalExact": False,
        "booking": {"url": "https://buchung.autozug-sylt.de/shop002/", "label": "Beim blauen RDC Autozug buchen", "affiliateParam": None,
                    # Partnerlink aus dem Kooperationsvertrag mit RDC (Sept. 2026). Eigenes Feld,
                    # damit ältere App-Versionen ohne Werbe-Hinweis beim normalen Link bleiben.
                    "affiliateUrl": "https://buchung.autozug-sylt.de/shop002/reflink?id=8000000094",
                    "priceFrom": "19,90 €"},
        "status": {"url": "https://www.autozug-sylt.de/de/fahrplan/"},
        # Zeigt die App nur an Tagen OHNE blaue Zeiten (RDC hat den Fahrplan noch
        # nicht veroeffentlicht) und nur im Zeitraum. Quelle: FAQ autozug-sylt.de,
        # abgerufen 25.09.2026. Keine Einzelzeiten erfinden – nur Belegtes.
        "noTimetableNote": {
            "text": "Ab dem 9. November 2026 gilt der Winterfahrplan. Letzte Abfahrt (Verladeschluss): ab Niebüll 17:35 Uhr, ab Westerland 17:55 Uhr.",
            "from": "2026-11-09",
            "to": "2027-03-19",
        },
    },
}
FLAG_LABELS = {"noMoto": "Keine Motorradbeförderung"}
RDC_TRAVEL_MINUTES = 45

TIME_RE = re.compile(r"([0-2]?\d:[0-5]\d)")


def fetch_bytes(url):
    """Wie fetch(), liefert aber die Rohdaten (fuer PDF-Downloads)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception:
        import subprocess
        return subprocess.run(["curl", "-sL", "-A", UA, "--max-time", "30", url],
                              capture_output=True, check=True).stdout


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


def rdc_pdf_urls(page_html):
    """Alle Fahrplan-PDFs der RDC-Seite – im Herbst stehen Sommer UND Winter dort."""
    hrefs = []
    for m in re.finditer(r'href="([^"]*Fahrplaene[^"]*\.pdf)"', page_html, re.I):
        href = m.group(1)
        url = href if href.startswith("http") else "https://www.autozug-sylt.de" + href
        if url not in hrefs:
            hrefs.append(url)
    if not hrefs:
        raise RuntimeError("RDC: kein Fahrplan-PDF verlinkt")
    return hrefs


# "GÜLTIG: 21. März bis 8. November 2026" bzw. "9. November 2026 bis 20. März 2027"
RDC_GUELTIG = re.compile(
    r"G(?:Ü|UE|ü)LTIG:?\s*(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s*(\d{4})?\s*(?:bis|–|-)\s*"
    r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s*(\d{4})", re.I)


def rdc_gueltigkeit(text):
    """Liest Zeitraum und Saison aus dem PDF. Ohne Gueltigkeitszeile: Fehler –
    lieber keine neuen Daten als geratene Saisons."""
    m = RDC_GUELTIG.search(text)
    if not m:
        raise RuntimeError("RDC: keine GÜLTIG-Zeile im Fahrplan-PDF")
    t1, m1, j1, t2, m2, j2 = m.groups()
    mon1, mon2 = MONATE.get(m1.capitalize()), MONATE.get(m2.capitalize())
    if not mon1 or not mon2:
        raise RuntimeError(f"RDC: Monat nicht erkannt in {m.group(0)!r}")
    j2 = int(j2)
    # Fehlt das erste Jahr, liegt es im selben Jahr – ausser der Zeitraum
    # reicht ueber den Jahreswechsel (Winter).
    j1 = int(j1) if j1 else (j2 - 1 if mon1 > mon2 else j2)
    von = datetime.date(j1, mon1, int(t1))
    bis = datetime.date(j2, mon2, int(t2))
    if bis <= von:
        raise RuntimeError(f"RDC: Zeitraum unplausibel: {von} bis {bis}")
    # Winter = der Zeitraum enthaelt einen Dezember- oder Januartag.
    tag, winter = von, False
    while tag <= bis:
        if tag.month in (12, 1):
            winter = True
            break
        tag += datetime.timedelta(days=1)
    return ("winter" if winter else "summer"), von.isoformat(), bis.isoformat()


def rdc_pdf_lesen(url):
    """Laedt das PDF und liest daraus einen Teilplan (braucht pdftotext/pdftoppm
    aus poppler). Liefert {season, von, bis, text, fahrten}; jede Fahrt traegt
    die Menge der Kalendertage, an denen sie laut PDF faehrt."""
    import tempfile
    data = fetch_bytes(url)
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "plan.pdf")
        txt = os.path.join(tmp, "plan.txt")
        with open(pdf, "wb") as f:
            f.write(data)
        subprocess.run(["pdftotext", "-layout", pdf, txt], check=True,
                       capture_output=True)
        with open(txt, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        season, von, bis = rdc_gueltigkeit(text)
        plan = {"url": url, "season": season, "von": von, "bis": bis, "text": text}
        if RDC_RASTER_KOPF.search(text):
            plan["fahrten"] = rdc_raster_fahrten(pdf, tmp, von, bis)
        else:
            plan["fahrten"] = rdc_text_fahrten(text, von, bis)
    return plan


# "4:30   5:20 Mo.-Fr. | *1"  bzw.  "6:05   6:50 taeglich"
RDC_ROW = re.compile(
    r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+(täglich|Mo\.-Fr\.|Fr\.|Sa\.|So\.)\s*(?:\|\s*\*(\d))?")
# Jedes Zeitpaar im Text – damit faellt eine Zeile mit unbekannter Tagesregel auf,
# statt still zu verschwinden.
RDC_ZEITPAAR = re.compile(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})")
# Kopfzeile eines Ankreuz-Fahrplans (Weihnachten): "24.12   25.12   26.12 ..."
RDC_RASTER_KOPF = re.compile(r"(?:\b\d{2}\.\d{2}\b\s+){4,}\d{2}\.\d{2}\b")

# Fussnote: "*3 1. Mai und 3. & 31. Oktober 2026."  -> Ausnahmedaten
MONATE = {"Januar":1,"Februar":2,"März":3,"April":4,"Mai":5,"Juni":6,"Juli":7,
          "August":8,"September":9,"Oktober":10,"November":11,"Dezember":12}


def rdc_datumsliste(klar):
    """ "1. & 2. April sowie 13., 21. & 22. Mai 2026" -> ISO-Tage. Ohne Jahr: leer."""
    jahr = re.search(r"\b(20\d\d)\b", klar)
    tage = []
    if jahr:
        for tm in re.finditer(r"((?:\d{1,2}\.\s*(?:&|und|,)?\s*)+)([A-ZÄÖÜ][a-zäöü]+)", klar):
            monat = MONATE.get(tm.group(2))
            if not monat:
                continue
            for t in re.findall(r"(\d{1,2})\.", tm.group(1)):
                tage.append(f"{jahr.group(1)}-{monat:02d}-{int(t):02d}")
    return sorted(set(tage))


def rdc_fussnoten(text):
    """Ordnet jeder Fussnote die Kalendertage zu, an denen der Zug NICHT faehrt.

    Die Hinweise stehen im PDF in einer eigenen Spalte rechts, und der Verweis
    ("*3") sitzt mitten im Satz. Deshalb wird erst die rechte Spalte
    herausgeschnitten und dann an "Zug verkehrt nicht" in Bloecke zerlegt.
    """
    zeilen = text.split("\n")
    spalte = None
    for z in zeilen:
        i = z.find("WICHTIGE HINWEISE")
        if i >= 0:
            spalte = max(0, i - 12)
            break
    if spalte is None:
        return {}
    rechts = " ".join(z[spalte:].strip() for z in zeilen if len(z) > spalte)

    noten = {}
    for block in re.split(r"Zug verkehrt nicht", rechts):
        m = re.search(r"\*(\d)", block)
        if not m:
            continue
        nr = m.group(1)
        noten[nr] = rdc_datumsliste(block.replace("*" + nr, " "))
    return noten


def rdc_services(page_html):
    """Liest den blauen Fahrplan aus ALLEN verlinkten PDFs – vollstaendig und
    tagesunabhaengig. Liefert (Fahrten, Saison-Zeitraeume fuer operatorSeasons).

    Die HTML-Seite zeigt nur die Abfahrten des gerade gewaehlten Datums und
    verschweigt die Wochentagsregeln; sie taugt daher nicht als Quelle.

    RDC teilt den Winter in mehrere PDFs (Herbst 2026: Uebergang 9.11.–12.12.,
    Winter 13.12.–19.3., Weihnachten 24.12.–3.1.). Die Apps kennen aber nur
    "summer"/"winter" – und lehnen Unbekanntes ab. Deshalb werden alle Teilplaene
    einer Saison zu EINER Saison zusammengefuehrt; welche Fahrt an welchem Tag
    faehrt, steckt in days + exceptDates (versteht auch jede aeltere App-Version).
    """
    nach_saison = {}
    for url in rdc_pdf_urls(page_html):
        plan = rdc_pdf_lesen(url)
        if not plan["fahrten"]:
            raise RuntimeError(f"RDC: keine Fahrten gelesen aus {url}")
        nach_saison.setdefault(plan["season"], []).append(plan)

    services, seasons = [], {}
    for season, plaene in nach_saison.items():
        s, ranges = rdc_saison_zusammenfuehren(season, plaene)
        services += s
        seasons[season] = {"ranges": ranges}
    return services, seasons


def _tage(von, bis):
    """Alle Kalendertage von..bis (ISO-Strings, inklusive) als ISO-Strings."""
    a, b = datetime.date.fromisoformat(von), datetime.date.fromisoformat(bis)
    return [(a + datetime.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def app_tagestyp(tag):
    """Exakt die Regel der Apps (TimetableEngine.dayType): Feiertag = Wochenende."""
    if tag in HOLIDAYS:
        return "weekend"
    return "weekend" if datetime.date.fromisoformat(tag).weekday() >= 5 else "weekday"


def _faehrt_nach_regel(tag, regel):
    """Wochentagsregel aus dem PDF. "Mo.-Fr." und "Fr." fahren wie bisher nicht
    an Feiertagen (= Tagestyp "weekday" der Apps)."""
    wt = datetime.date.fromisoformat(tag).weekday()
    if regel == "täglich":
        return True
    if regel == "Mo.-Fr.":
        return app_tagestyp(tag) == "weekday"
    if regel == "Fr.":
        return wt == 4 and app_tagestyp(tag) == "weekday"
    if regel == "Sa.":
        return wt == 5
    if regel == "So.":
        return wt == 6
    raise RuntimeError(f"RDC: unbekannte Tagesregel {regel!r}")


def rdc_text_fahrten(text, von, bis):
    """Teilplan mit Spalte WOCHENTAGE (Sommer, Uebergang, Winter)."""
    noten = rdc_fussnoten(text)
    tage = _tage(von, bis)
    fahrten = []
    zeilen = text.split("\n")
    for zi, z in enumerate(zeilen):
        treffer = list(RDC_ROW.finditer(z))
        paare = list(RDC_ZEITPAAR.finditer(z))
        if len(paare) == 1 and not treffer:
            # Sonderfahrt nur an genannten Tagen, z. B. Sommer 2026:
            #   "                1. & 2. April sowie"
            #   "20:05   20:50   13., 21. & 22. Mai 2026"
            # Die Liste kann in der Zeile darueber beginnen.
            m = paare[0]
            liste = z[m.end():].strip()
            davor = zeilen[zi - 1].strip() if zi else ""
            if davor and not RDC_ZEITPAAR.search(davor) and not TIME_RE.search(davor):
                liste = davor + " " + liste
            tage_liste = rdc_datumsliste(liste)
            if not tage_liste:
                raise RuntimeError(f"RDC: Zeile mit unbekannter Tagesregel: {z.strip()!r}")
            direction = "toIsland" if m.start() < len(z) // 2 else "toMainland"
            fahrten.append({"dir": direction, "close": norm(m.group(1)), "arr": norm(m.group(2)),
                            "flags": [], "tage": set(tage_liste) & set(tage)})
            continue
        if len(paare) != len(treffer):
            raise RuntimeError(f"RDC: Zeile mit unbekannter Tagesregel: {z.strip()!r}")
        # linke Spalte = Niebuell->Westerland, rechte = Westerland->Niebuell
        for m in treffer:
            direction = "toIsland" if m.start() < len(z) // 2 else "toMainland"
            close, arr, regel, note = m.group(1), m.group(2), m.group(3), m.group(4)
            if note and note not in noten:
                raise RuntimeError(f"RDC: Fussnote *{note} nicht gefunden: {z.strip()!r}")
            aus = set(noten.get(note, [])) if note else set()
            fahrten.append({"dir": direction, "close": norm(close), "arr": norm(arr),
                            "flags": [],
                            "tage": {t for t in tage if _faehrt_nach_regel(t, regel) and t not in aus}})
    return fahrten


def _pgm_lesen(pfad):
    """Minimaler Leser fuer binaere Graustufen-PGM (P5) aus pdftoppm -gray."""
    with open(pfad, "rb") as f:
        roh = f.read()
    felder, pos = [], 0
    while len(felder) < 4:
        while roh[pos:pos + 1].isspace():
            pos += 1
        if roh[pos:pos + 1] == b"#":
            pos = roh.index(b"\n", pos) + 1
            continue
        start = pos
        while not roh[pos:pos + 1].isspace():
            pos += 1
        felder.append(roh[start:pos])
    if felder[0] != b"P5" or int(felder[3]) != 255:
        raise RuntimeError("RDC: unerwartetes Rasterformat")
    breite, hoehe = int(felder[1]), int(felder[2])
    return breite, hoehe, roh[pos + 1:pos + 1 + breite * hoehe]


RASTER_DPI = 144          # 2 Pixel je PDF-Punkt
HAKEN_FENSTER = 3.5       # halbe Kantenlaenge des Pruef-Quadrats in Punkt
HAKEN_LEER_MAX = 0.03     # Anteil dunkler Pixel: leer bis hier ...
HAKEN_VOLL_MIN = 0.12     # ... Haken ab hier; dazwischen = unsicher -> Abbruch


def rdc_raster_fahrten(pdf, tmp, von, bis):
    """Teilplan als Ankreuz-Tabelle (Weihnachten): Zeilen = Zuege, Spalten = Tage,
    ein Haken = Zug faehrt. Die Haken sind Grafik, kein Text. Darum: Positionen
    der Tage und Zeiten aus pdftotext -bbox, dann im gerasterten Bild je Zelle
    dunkle Pixel zaehlen. Jede Zelle muss eindeutig leer oder angekreuzt sein,
    sonst Abbruch – lieber keine Daten als geratene."""
    bbox = os.path.join(tmp, "bbox.html")
    subprocess.run(["pdftotext", "-bbox", pdf, bbox], check=True, capture_output=True)
    with open(bbox, encoding="utf-8", errors="ignore") as f:
        woerter = [(float(a), float(b), float(c), float(d), htmllib.unescape(w))
                   for a, b, c, d, w in re.findall(
                       r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', f.read())]
    subprocess.run(["pdftoppm", "-r", str(RASTER_DPI), "-gray", "-singlefile", pdf,
                    os.path.join(tmp, "raster")], check=True, capture_output=True)
    breite, hoehe, pixel = _pgm_lesen(os.path.join(tmp, "raster.pgm"))
    px = RASTER_DPI / 72.0

    def dunkel_anteil(xc, yc):
        x0, x1 = int((xc - HAKEN_FENSTER) * px), int((xc + HAKEN_FENSTER) * px)
        y0, y1 = int((yc - HAKEN_FENSTER) * px), int((yc + HAKEN_FENSTER) * px)
        n = d = 0
        for y in range(max(0, y0), min(hoehe, y1)):
            zeile = pixel[y * breite:(y + 1) * breite]
            for x in range(max(0, x0), min(breite, x1)):
                n += 1
                d += zeile[x] < 110
        return d / n if n else 0.0

    # Tagesspalten je Block: Woerter "dd.mm", nach Zeilenhoehe gruppiert.
    kopf = {}
    for x0, y0, x1, y1, w in woerter:
        if re.fullmatch(r"\d{2}\.\d{2}", w):
            kopf.setdefault(round(y0 / 5), []).append(((x0 + x1) / 2, w, y0, y1))
    bloecke = sorted(kopf.values(), key=lambda k: k[0][2])
    if len(bloecke) != 2:
        raise RuntimeError(f"RDC-Ankreuzplan: {len(bloecke)} statt 2 Tabellen gefunden")

    alle_tage = _tage(von, bis)
    def iso(ddmm):
        treffer = [t for t in alle_tage if t[8:10] + "." + t[5:7] == ddmm]
        if len(treffer) != 1:
            raise RuntimeError(f"RDC-Ankreuzplan: Tag {ddmm} liegt nicht im Zeitraum {von}–{bis}")
        return treffer[0]

    fahrten = []
    for bi, spalten in enumerate(bloecke):
        spalten.sort()
        tage = [iso(w) for _, w, _, _ in spalten]
        if tage != alle_tage:
            raise RuntimeError(f"RDC-Ankreuzplan: Spalten {tage} decken {von}–{bis} nicht lueckenlos ab")
        kopf_oben, kopf_unten = min(s[2] for s in spalten), max(s[3] for s in spalten)
        ende = min(s[2] for s in bloecke[1]) if bi == 0 else 1e9
        # Richtung aus der Ueberschrift direkt ueber der Tabelle.
        ueber = [w for w in woerter if kopf_oben - 30 < w[1] < kopf_oben]
        ni = [w[0] for w in ueber if w[4].upper().startswith("NIEB")]
        we = [w[0] for w in ueber if w[4].upper().startswith("WESTERLAND")]
        if len(ni) != 1 or len(we) != 1:
            raise RuntimeError("RDC-Ankreuzplan: Richtung nicht erkennbar")
        direction = "toIsland" if ni[0] < we[0] else "toMainland"
        # Zeilen: Zeitwoerter zwischen Kopf und naechster Tabelle, nach Hoehe gruppiert.
        zeiten = sorted(((y0 + y1) / 2, x0, w) for x0, y0, x1, y1, w in woerter
                        if kopf_unten < y0 < ende and TIME_RE.fullmatch(w))
        zeilen = []
        for yc, x0, w in zeiten:
            if zeilen and yc - zeilen[-1][0][2] < 3:   # gleiche Tabellenzeile
                zeilen[-1].append((x0, w, yc))
            else:
                zeilen.append([(x0, w, yc)])
        if len(zeilen) < 8:
            raise RuntimeError(f"RDC-Ankreuzplan: nur {len(zeilen)} Zeilen in Tabelle {bi + 1}")
        for zeit in zeilen:
            if len(zeit) != 2:
                raise RuntimeError(f"RDC-Ankreuzplan: Zeile ohne Zeitpaar: {zeit}")
            zeit.sort()
            yc = zeit[0][2]
            faehrt = set()
            for (xc, _, _, _), tag in zip(spalten, tage):
                a = dunkel_anteil(xc, yc)
                if os.environ.get("RDC_RASTER_DEBUG"):
                    print(f"  raster {direction} {zeit[0][1]} {tag}: {a:.3f}", file=sys.stderr)
                if HAKEN_LEER_MAX < a < HAKEN_VOLL_MIN:
                    raise RuntimeError(f"RDC-Ankreuzplan: Zelle {zeit[0][1]} / {tag} unklar ({a:.2f})")
                if a >= HAKEN_VOLL_MIN:
                    faehrt.add(tag)
            fahrten.append({"dir": direction, "close": norm(zeit[0][1]), "arr": norm(zeit[1][1]),
                            "flags": [], "tage": faehrt})
    return fahrten


def _monatstext(iso_tag):
    d = datetime.date.fromisoformat(iso_tag)
    name = [k for k, v in MONATE.items() if v == d.month][0]
    return rf"{d.day}\.\s*{name}"


def rdc_saison_zusammenfuehren(season, plaene):
    """Fuehrt die Teilplaene einer Saison zu einer Fahrtenliste zusammen.

    Vorrang: Liegt ein Teilplan ganz INNERHALB eines anderen und kuendigt der
    aeussere ihn selbst an ("Vom 24. Dezember ... bis 3. Januar gilt ein
    gesonderter ..."), gilt an diesen Tagen nur der innere. Jede andere
    Ueberlappung ist ohne Vorrangregel -> Abbruch, statt zu raten."""
    vorrang = {i: set() for i in range(len(plaene))}   # i wird von diesen Plaenen ueberstimmt
    for i, a in enumerate(plaene):
        for j, b in enumerate(plaene):
            if j <= i or a["bis"] < b["von"] or b["bis"] < a["von"]:
                continue
            for innen, aussen, ii, aa in ((a, b, i, j), (b, a, j, i)):
                if aussen["von"] <= innen["von"] and innen["bis"] <= aussen["bis"] \
                        and (innen["von"], innen["bis"]) != (aussen["von"], aussen["bis"]):
                    hinweis = _monatstext(innen["von"]) + r"\s*(?:\d{4})?\s*bis\s*" + _monatstext(innen["bis"])
                    if re.search(hinweis, " ".join(aussen["text"].split())):
                        vorrang[aa].add(ii)
                        break
            else:
                raise RuntimeError(
                    f"RDC: Teilplaene {a['von']}–{a['bis']} und {b['von']}–{b['bis']} "
                    f"ueberlappen ohne Vorrangregel")

    fahrten = {}
    alle = set()
    for i, p in enumerate(plaene):
        gilt = set(_tage(p["von"], p["bis"]))
        for j in vorrang[i]:
            gilt -= set(_tage(plaene[j]["von"], plaene[j]["bis"]))
        alle |= set(_tage(p["von"], p["bis"]))
        for f in p["fahrten"]:
            key = (f["dir"], f["close"], f["arr"], tuple(f["flags"]))
            fahrten.setdefault(key, set()).update(f["tage"] & gilt)

    # Zusammenhaengende Zeitraeume fuer operatorSeasons.
    tage = sorted(alle)
    ranges = [{"from": tage[0], "to": tage[0]}]
    for t in tage[1:]:
        if datetime.date.fromisoformat(t) - datetime.date.fromisoformat(ranges[-1]["to"]) == datetime.timedelta(days=1):
            ranges[-1]["to"] = t
        else:
            ranges.append({"from": t, "to": t})

    services = []
    for (direction, close, arr, flags), faehrt in fahrten.items():
        if not faehrt:
            continue
        # Kuerzeste Darstellung: Tagestyp der Apps + die Tage, an denen sie trotzdem
        # nicht faehrt. Jede Fahrt wird danach gegen die App-Logik nachgerechnet.
        beste = None
        for days in ("all", "weekday", "weekend"):
            basis = {t for t in tage if days == "all" or app_tagestyp(t) == days}
            if faehrt <= basis and (beste is None or len(basis - faehrt) < len(beste[1])):
                beste = (days, sorted(basis - faehrt))
        days, aus = beste
        eintrag = {"op": "rdc", "dir": direction, "season": season, "days": days,
                   "close": close, "arr": arr, "arrExact": False, "flags": list(flags)}
        if aus:
            eintrag["exceptDates"] = aus
        nachgerechnet = {t for t in tage
                         if (days == "all" or app_tagestyp(t) == days) and t not in aus}
        if nachgerechnet != faehrt:
            raise RuntimeError(f"RDC: Darstellung von {direction} {close} stimmt nicht")
        services.append(eintrag)
    return services, ranges


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


def build_doc(services, data_version, data_dates, operator_seasons):
    services = sorted(services, key=lambda s: (s["op"], s["dir"], s["season"], s["close"]))
    return {
        "schemaVersion": 1,
        "dataVersion": data_version,
        "generatedAt": data_version + "T06:00:00Z",
        "note": "Alle Angaben ohne Gewähr. Zeiten = Verladeschluss (Check-in). "
                "Unabhängige App, keine offizielle App von DB oder RDC.",
        "seasons": SEASONS,
        "operatorSeasons": operator_seasons,
        "holidays": HOLIDAYS,
        "operators": OPERATORS,
        "flagLabels": FLAG_LABELS,
        "services": services,
        "dataDates": data_dates,
    }


def services_signature(doc):
    """Fingerabdruck des GESAMTEN Inhalts – nur die Zeitstempel bleiben aussen vor.

    Vorher wurden nur die Fahrten verglichen. Dadurch wurde eine Korrektur an den
    Metadaten (z. B. eine falsche Betreiber-URL) nie veröffentlicht: Die Fahrten
    waren unverändert, also gab es keinen Commit – der Fix blieb für immer liegen.
    """
    ignored = {"dataVersion", "generatedAt", "dataDates"}
    return json.dumps({k: v for k, v in doc.items() if k not in ignored},
                      sort_keys=True, ensure_ascii=False)


def operator_signature(doc, op):
    """Fingerabdruck nur der Fahrten EINES Betreibers.

    Damit bekommt jeder Betreiber sein eigenes Änderungsdatum: Ändert die DB
    ihren Plan, soll beim blauen Zug nicht so aussehen, als sei er auch neu.
    """
    rows = [s for s in doc.get("services", []) if s.get("op") == op]
    return json.dumps(sorted(rows, key=lambda s: (s["dir"], s["season"], s["close"])),
                      sort_keys=True, ensure_ascii=False)


def rdc_laufende_behalten(old_doc, services, seasons, today):
    """Nimmt RDC einen noch gueltigen Fahrplan von der Seite (z. B. das Sommer-PDF,
    sobald der Winter erscheint, aber vor dem 8. November), bleiben dessen Fahrten
    bis zum Ende seines Zeitraums erhalten – sonst stuende der blaue Zug fuer die
    Restwochen ohne Zeiten da."""
    alt = ((old_doc or {}).get("operatorSeasons") or {}).get("rdc") or {}
    for season, dto in alt.items():
        if season in seasons:
            continue
        ende = max((r["to"] for r in dto.get("ranges", [])), default="")
        if ende >= today:
            seasons[season] = dto
            services += [s for s in old_doc.get("services", [])
                         if s.get("op") == "rdc" and s.get("season") == season]
    return services, seasons


def merge_data_dates(old_doc, new_doc, today):
    """Datum je Betreiber nur dort hochsetzen, wo sich wirklich etwas geändert hat."""
    old_dates = (old_doc or {}).get("dataDates") or {}
    fallback = (old_doc or {}).get("dataVersion") or today
    out = {}
    for op in OPERATORS:
        if old_doc is None:
            out[op] = today
        elif operator_signature(old_doc, op) != operator_signature(new_doc, op):
            out[op] = today
        else:
            out[op] = old_dates.get(op, fallback)
    return out


def main():
    check_only = "--check" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "timetable.json")

    try:
        rdc, rdc_seasons = rdc_services(fetch(RDC_URL))
        services = db_services(fetch(DB_INDEX)) + rdc
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

    old_doc = None
    old_sig = None
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old_doc = json.load(f)
        old_sig = services_signature(old_doc)

    services, rdc_seasons = rdc_laufende_behalten(old_doc, services, rdc_seasons, today)
    operator_seasons = {"rdc": rdc_seasons}

    # Erst ohne Daten bauen, dann je Betreiber gegen den alten Stand vergleichen.
    probe = build_doc(services, today, {}, operator_seasons)
    data_dates = merge_data_dates(old_doc, probe, today)
    new_doc = build_doc(services, today, data_dates, operator_seasons)
    for season, dto in sorted(rdc_seasons.items()):
        print(f"  RDC {season}: {dto['ranges'][0]['from']} bis {dto['ranges'][-1]['to']}")

    new_sig = services_signature(new_doc)
    changed = new_sig != old_sig
    print(f"Geparst: {len(services)} Fahrten. Änderung: {'JA' if changed else 'nein'}.")
    for op, d in sorted(data_dates.items()):
        print(f"  Datenstand {op}: {d}")

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
