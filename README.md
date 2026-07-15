# Sylt Autozüge – Fahrplandaten 🚂🔴🔵

Öffentliches Daten-Repo für die iOS-App **Sylt Autozüge** (kombinierter Fahrplan des roten
DB Sylt Shuttle und blauen RDC Autozug Sylt zwischen Niebüll und Westerland/Sylt).

- **`timetable.json`** — die Fahrplandaten, die die App per Raw-URL lädt.
- **`update_timetable.py`** — Scraper, der die offiziellen Fahrpläne (syltshuttle.de, autozug-sylt.de)
  ausliest, streng validiert und `timetable.json` aktualisiert.
- **`.github/workflows/update-timetable.yml`** — GitHub Action, die den Scraper wöchentlich
  (Mo 04:17 UTC) ausführt und Änderungen automatisch committet. Fail-Safe: bei Abruf-/
  Validierungsfehler bleibt die Datei unverändert und der Job schlägt fehl.
- **`datenschutz.html`** — Datenschutzerklärung der App (via GitHub Pages).

Manuell prüfen (ohne Schreiben):

```bash
python3 update_timetable.py --check
```

> Unabhängiges Projekt, keine offizielle App von DB oder RDC. Alle Angaben ohne Gewähr.
