#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prüft eine von Hand geänderte timetable.json, BEVOR sie zu GitHub geht.

Aufruf (im Ordner sylt-autozug-data):
    python3 pruefe_fahrplan.py

Prüft dieselben Regeln, nach denen die App eine Fahrplan-Datei annimmt oder verwirft
(TimetableData.isValid in der iOS-App). Meldet jede Stelle in Klartext.
Ende mit "ALLES IN ORDNUNG" = darf hochgeladen werden. Sonst: erst beheben.
"""
import json
import re
import subprocess
import sys
from datetime import date

DATEI = "timetable.json"
ZEIT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
fehler, hinweise = [], []


def tag(text):
    try:
        return date.fromisoformat(text)
    except Exception:
        return None


def zeitraeume(wo, ranges):
    if not ranges:
        fehler.append(f"{wo}: kein Zeitraum eingetragen")
    for r in ranges or []:
        von, bis = tag(r.get("from", "")), tag(r.get("to", ""))
        if not von or not bis:
            fehler.append(f"{wo}: Datum nicht lesbar ({r}) – Form JJJJ-MM-TT")
        elif von > bis:
            fehler.append(f"{wo}: 'from' liegt nach 'to' ({r})")


def main():
    try:
        d = json.load(open(DATEI, encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"{DATEI} nicht gefunden – bitte im Ordner sylt-autozug-data ausführen.")
    except json.JSONDecodeError as e:
        sys.exit(f"FEHLER: Die Datei ist kein gültiges JSON mehr.\n"
                 f"  Zeile {e.lineno}, Spalte {e.colno}: {e.msg}\n"
                 f"  Meist fehlt dort ein Komma, eine Klammer oder ein Anführungszeichen.")

    if d.get("schemaVersion") != 1:
        fehler.append("schemaVersion muss 1 bleiben")
    if not tag(d.get("dataVersion", "")):
        fehler.append("dataVersion muss ein Datum JJJJ-MM-TT sein")
    for op in ("db", "rdc"):
        if op not in d.get("operators", {}):
            fehler.append(f"operators: Eintrag '{op}' fehlt")
    for name, s in d.get("seasons", {}).items():
        if name not in ("summer", "winter"):
            fehler.append(f"seasons: unbekannter Name '{name}' (nur summer / winter)")
        zeitraeume(f"seasons.{name}", s.get("ranges"))
    for op, saisons in (d.get("operatorSeasons") or {}).items():
        if op not in ("db", "rdc"):
            fehler.append(f"operatorSeasons: unbekannter Betreiber '{op}'")
        for name, s in saisons.items():
            if name not in ("summer", "winter"):
                fehler.append(f"operatorSeasons.{op}: unbekannter Name '{name}'")
            zeitraeume(f"operatorSeasons.{op}.{name}", s.get("ranges"))
    for h in d.get("holidays", []):
        if not tag(h):
            fehler.append(f"holidays: Datum nicht lesbar: {h}")
    for op, wert in (d.get("dataDates") or {}).items():
        if op not in ("db", "rdc") or not tag(wert):
            fehler.append(f"dataDates: Eintrag '{op}': '{wert}' ist ungültig")

    fahrten = d.get("services", [])
    if not fahrten:
        fehler.append("services ist leer – die App hätte keinen einzigen Zug")
    for nr, s in enumerate(fahrten, 1):
        wo = f"Fahrt {nr} ({s.get('op')} {s.get('dir')} {s.get('season')} {s.get('close')})"
        if s.get("op") not in ("db", "rdc"):
            fehler.append(f"{wo}: op muss db oder rdc sein")
        if s.get("dir") not in ("toIsland", "toMainland"):
            fehler.append(f"{wo}: dir muss toIsland oder toMainland sein")
        if s.get("season") not in ("summer", "winter"):
            fehler.append(f"{wo}: season muss summer oder winter sein")
        if s.get("days") not in ("all", "weekday", "weekend"):
            fehler.append(f"{wo}: days muss all, weekday oder weekend sein")
        for feld in ("close", "arr"):
            if not ZEIT.match(str(s.get(feld, ""))):
                fehler.append(f"{wo}: {feld} muss die Form HH:MM haben, z. B. 07:05")
        if not isinstance(s.get("arrExact"), bool):
            fehler.append(f"{wo}: arrExact muss true oder false sein (ohne Anführungszeichen)")
        if not isinstance(s.get("flags"), list):
            fehler.append(f"{wo}: flags muss eine Liste sein, leer ist []")
        for t in s.get("exceptDates") or []:
            if not tag(t):
                fehler.append(f"{wo}: exceptDates enthält ein unlesbares Datum: {t}")

    # Derselbe Zug doppelt (gleiche Regel wie in der App und im Update-Skript).
    gruppen = {}
    for s in fahrten:
        gruppen.setdefault((s.get("op"), s.get("dir"), s.get("season"), s.get("close")), []).append(s)
    for schluessel, gruppe in gruppen.items():
        for i in range(len(gruppe)):
            for j in range(i + 1, len(gruppe)):
                a, b = gruppe[i], gruppe[j]
                tage = a.get("days") == b.get("days") or "all" in (a.get("days"), b.get("days"))
                ea, eb = a.get("exceptDates") or [], b.get("exceptDates") or []
                getrennt = bool(ea) and bool(eb) and ea != eb
                if tage and not getrennt:
                    fehler.append(f"Derselbe Zug steht doppelt drin: {' '.join(map(str, schluessel))} "
                                  f"– die App verwirft dann die GANZE Datei")

    # Nicht älter als das, was gerade online ist – sonst nimmt die App die Datei nicht.
    try:
        alt = json.loads(subprocess.run(["git", "show", "origin/main:" + DATEI], capture_output=True,
                                        text=True, check=True).stdout)
        if d.get("dataVersion", "") < alt.get("dataVersion", ""):
            fehler.append(f"dataVersion {d.get('dataVersion')} ist älter als die Online-Fassung "
                          f"({alt.get('dataVersion')}) – die App würde die Datei ignorieren")
        elif d.get("dataVersion") == alt.get("dataVersion") and d != alt:
            hinweise.append("Inhalt geändert, aber dataVersion ist gleich geblieben. Die App nimmt die "
                            "Datei an; setze dataVersion trotzdem auf das heutige Datum, dann stimmt "
                            "auch der angezeigte Fahrplanstand.")
        if d == alt:
            hinweise.append("Die Datei ist identisch mit der Online-Fassung – es gibt nichts hochzuladen.")
    except Exception:
        hinweise.append("Vergleich mit der Online-Fassung nicht möglich (kein Netz/kein git).")

    heute = date.today().isoformat()
    kommende = [h for h in d.get("holidays", []) if h >= heute]
    if len(kommende) < 3:
        hinweise.append(f"Nur noch {len(kommende)} kommende Feiertage eingetragen – bitte die Feiertage "
                        f"des nächsten Jahres nachtragen (Anleitung Kapitel 20).")

    print(f"{len(fahrten)} Fahrten, Datenstand {d.get('dataVersion')}")
    for h in hinweise:
        print("Hinweis:", h)
    if fehler:
        print(f"\n{len(fehler)} FEHLER – NICHT hochladen:")
        for f in fehler[:40]:
            print("  ✗", f)
        sys.exit(1)
    print("\nALLES IN ORDNUNG – die Datei darf hochgeladen werden.")


if __name__ == "__main__":
    main()
