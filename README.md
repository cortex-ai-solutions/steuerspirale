# Steuerspirale

Interaktive Visualisierung der kassenmäßigen Steuereinnahmen Deutschlands — als Archimedische Spirale nach dem Vorbild des Bundesministeriums der Finanzen (BMF).

**Live:** [cortex-ai-solutions.github.io/steuerspirale](https://cortex-ai-solutions.github.io/steuerspirale/)

---

## Funktionsumfang

- Archimedische Spirale mit D3.js v7 — die 28 größten Steuerarten als Kreise
- Kreisgröße proportional zum Steueraufkommen (Wurzel-/Log-/Lineare Skalierung wählbar)
- Beschriftung adaptiv: Name + Wert **innerhalb** großer Kreise, außen bei kleinen
- Hover-Tooltip mit Betrag, Anteil am Gesamtaufkommen und Rang
- Jahres-Selektor: 2021 – 2025 (live aus GENESIS-API)
- Spiralstart-Regler (0–359°, Standard 0° = 12-Uhr-Position wie BMF-Original)
- Sidebar mit vollständiger Rangliste aller Steuerarten
- Umsatzsteuer und Einfuhrumsatzsteuer werden als kombinierter Wert ausgewiesen

---

## Daten aktualisieren

Voraussetzung: GENESIS-Online-Zugangsdaten als Umgebungsvariable `mein_genesis_key` (Token oder `KENNUNG:PASSWORT`).

```bash
# Aktuellstes verfügbares Jahr (auto-detect)
python fetch_data.py

# Bestimmtes Jahr
python fetch_data.py --year 2024

# Verbindungstest
python fetch_data.py --check
```

Die Skripte schreiben `data.json` (aktuellstes Jahr) und `data_{year}.json` (jahresspezifisch).

---

## Automatischer Datenabruf (GitHub Actions)

Der Workflow `.github/workflows/update-data.yml` läuft automatisch am 1. jedes Monats und commitet aktualisierte Daten direkt ins Repository.

Manueller Trigger über GitHub → Actions → *Update Steuerspirale Data* → *Run workflow*.

Erforderliches Repository-Secret: `MEIN_GENESIS_KEY`

---

## Datenquelle

Statistisches Bundesamt (Destatis), GENESIS-Online, Tabelle **71211-0001**  
Kassenmäßige Steuereinnahmen nach Steuerarten und Gebietskörperschaften

---

## Abhängigkeiten

```
requests>=2.28
python-dotenv>=1.0
```

Installation: `pip install -r requirements.txt`
