#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          STEUERSPIRALE — Daten-Abruf-Service                    ║
║  Statistisches Bundesamt · GENESIS-Online API · Tabelle 71211-0001 ║
╚══════════════════════════════════════════════════════════════════╝

Ruft Steuereinnahmen aus GENESIS-Online (Destatis) ab und erzeugt
JSON-Dateien für die D3.js-Steuerspirale.

VERWENDUNG:
  python fetch_data.py                          # auto-detect aktuellstes Jahr
  python fetch_data.py --year 2023              # bestimmtes Jahr
  python fetch_data.py --year latest            # explizit: aktuellstes Jahr
  python fetch_data.py --api-key KENNUNG:PW     # Key per Argument
  python fetch_data.py --mock                   # (nur Entwicklung) Mock-Daten

API-KEY:
  Umgebungsvariable: mein_genesis_key=KENNUNG:PASSWORT
  Registrierung: https://www-genesis.destatis.de/genesis/online → Benutzerprofil

AUSGABE:
  data.json            — aktuellstes Jahr (Standard-Ladeadresse der HTML-Seite)
  data_{year}.json     — jahresspezifische Datei (z.B. data_2024.json)

DEPLOYMENT (Elestio / GitHub Actions):
  Umgebungsvariable 'mein_genesis_key' setzen, dann:
  python fetch_data.py --year latest --output data.json
"""

import json
import sys
import os
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.parse
    HAS_REQUESTS = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("steuerspirale")


# ─── MOCK-DATEN (nur für --mock / lokale Entwicklung) ───────────────────────
MOCK_DATA_2024 = [
    {"name": "Umsatz- und Mehrwertsteuer",  "value": 302143},
    {"name": "Lohnsteuer",                  "value": 248920},
    {"name": "Gewerbesteuer",               "value":  75294},
    {"name": "Einkommensteuer",             "value":  74845},
    {"name": "Körperschaftsteuer",          "value":  39758},
    {"name": "Energiesteuer",               "value":  35087},
    {"name": "Nicht veranlagte Steuern",    "value":  34023},
    {"name": "Abgeltungsteuer",             "value":  19267},
    {"name": "Versicherungsteuer",          "value":  18227},
    {"name": "Grundsteuern",                "value":  16066},
    {"name": "Tabaksteuer",                 "value":  15637},
    {"name": "Grunderwerbsteuer",           "value":  12750},
    {"name": "Solidaritätszuschlag",        "value":  12634},
    {"name": "Erbschaftsteuer",             "value":   9990},
    {"name": "Kfz-Steuer",                  "value":   9667},
    {"name": "Zölle",                       "value":   5463},
    {"name": "Stromsteuer",                 "value":   5153},
    {"name": "Alkoholsteuer",               "value":   1980},
    {"name": "EU-Krisenbeitrag",            "value":   1936},
    {"name": "Luftverkehrsteuer",           "value":   1833},
    {"name": "Lotteriesteuer",              "value":   1807},
    {"name": "Kaffeesteuer",                "value":    992},
    {"name": "Vergnügungsteuer",            "value":    900},
    {"name": "Feuerschutzsteuer",           "value":    724},
    {"name": "Biersteuer",                  "value":    558},
    {"name": "Hundesteuer",                 "value":    430},
    {"name": "Sportwettensteuer",           "value":    423},
    {"name": "Schaumweinsteuer",            "value":    352},
    {"name": "Zweitwohnungsteuer",          "value":    282},
]


# ─── GENESIS-ONLINE API ──────────────────────────────────────────────────────
GENESIS_BASE_URL = "https://www-genesis.destatis.de/genesisWS/rest/2020"
GENESIS_TABLE    = "71211-0001"


def parse_api_key(api_key_str: str) -> tuple[str, str]:
    """Erwartet 'KENNUNG:PASSWORT'."""
    if ":" in api_key_str:
        username, password = api_key_str.split(":", 1)
        return username.strip(), password.strip()
    log.error("API-Key muss das Format 'KENNUNG:PASSWORT' haben.")
    sys.exit(1)


def _get(url: str, params: dict, timeout: int = 30) -> dict:
    """HTTP GET, liefert JSON-Dict. Nutzt requests wenn verfügbar."""
    if HAS_REQUESTS:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    full_url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full_url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_genesis_response(raw: dict, year: int) -> Optional[list[dict]]:
    """
    Parst GENESIS-REST-API-Antwort (v2020) in [{name, value}]-Liste.

    Erwartete Struktur:
      {
        "Status":  {"Code": 0, "Content": "..."},
        "Object":  {
          "Array": [
            {"Cells": [{"Value": "Steuerart"}, ..., {"Value": "12345,6"}]}
          ]
        }
      }
    Werte kommen in Tausend € → Division durch 1000 → Mio. €
    """
    status = raw.get("Status", {})
    code   = status.get("Code", -1)
    if code != 0:
        log.error(f"GENESIS-Status {code}: {status.get('Content', '?')}")
        return None

    obj = raw.get("Object", {})
    if not obj:
        log.error("GENESIS-Antwort enthält kein 'Object'-Feld.")
        return None

    results = []
    rows = obj.get("Array", [])
    if not rows:
        log.warning("GENESIS 'Object.Array' ist leer.")
        return None

    for row in rows:
        cells = row.get("Cells", [])
        if len(cells) < 2:
            continue
        name  = str(cells[0].get("Value", "")).strip()
        raw_v = str(cells[-1].get("Value", "")).strip()
        if not name or raw_v in ("", "-", ".", "x"):
            continue
        try:
            # Deutsche Zahlenformatierung: "248.920,4" → float
            value_tsd = float(raw_v.replace(".", "").replace(",", "."))
            value_mio = round(value_tsd / 1000, 1)
            if value_mio > 0:
                results.append({"name": name, "value": value_mio})
        except (ValueError, TypeError):
            log.debug(f"Überspringe Zeile mit nicht-numerischem Wert: {raw_v!r}")
            continue

    if not results:
        log.warning(f"Keine auswertbaren Zeilen aus GENESIS für Jahr {year}.")
        return None

    log.info(f"{len(results)} Steuerarten aus GENESIS extrahiert (Jahr {year}).")
    return results


def fetch_genesis_table(api_key: str, year: int) -> Optional[list[dict]]:
    """Primärer Endpunkt: /data/table"""
    username, password = parse_api_key(api_key)
    endpoint = f"{GENESIS_BASE_URL}/data/table"
    params = {
        "username":   username,
        "password":   password,
        "name":       GENESIS_TABLE,
        "area":       "all",
        "compress":   "false",
        "transpose":  "false",
        "startyear":  str(year),
        "endyear":    str(year),
        "language":   "de",
        "format":     "json",
    }
    log.info(f"GENESIS /data/table  Benutzer={username}  Jahr={year}")
    try:
        return parse_genesis_response(_get(endpoint, params), year)
    except Exception as exc:
        log.warning(f"/data/table Fehler: {exc}")
        return None


def fetch_genesis_timeseries(api_key: str, year: int) -> Optional[list[dict]]:
    """Fallback-Endpunkt: /data/timeseries"""
    username, password = parse_api_key(api_key)
    endpoint = f"{GENESIS_BASE_URL}/data/timeseries"
    params = {
        "username":  username,
        "password":  password,
        "name":      GENESIS_TABLE,
        "area":      "all",
        "startyear": str(year),
        "endyear":   str(year),
        "language":  "de",
        "format":    "json",
    }
    log.info(f"GENESIS /data/timeseries  Benutzer={username}  Jahr={year}")
    try:
        return parse_genesis_response(_get(endpoint, params), year)
    except Exception as exc:
        log.warning(f"/data/timeseries Fehler: {exc}")
        return None


def fetch_year(api_key: str, year: int) -> Optional[list[dict]]:
    """Versucht beide Endpunkte; gibt None zurück wenn beide fehlschlagen."""
    data = fetch_genesis_table(api_key, year)
    if not data:
        log.info("Primärer Endpunkt lieferte keine Daten, versuche Zeitreihen-Endpunkt …")
        data = fetch_genesis_timeseries(api_key, year)
    return data


def get_latest_year_with_data(api_key: str) -> tuple[int, list[dict]]:
    """
    Probiert das aktuelle und die beiden Vorjahre durch.
    Gibt (year, data) zurück oder beendet das Programm mit Fehler.
    """
    current = datetime.now().year
    for year in range(current, current - 3, -1):
        log.info(f"Prüfe Jahr {year} …")
        data = fetch_year(api_key, year)
        if data:
            log.info(f"✔ Aktuellstes verfügbares Jahr: {year}")
            return year, data
    log.error("Keine GENESIS-Daten für die letzten 3 Jahre gefunden. Abbruch.")
    sys.exit(1)


# ─── OUTPUT ──────────────────────────────────────────────────────────────────
def build_output(data: list[dict], year: int, source: str) -> dict:
    sorted_data = sorted(data, key=lambda x: x["value"], reverse=True)
    total = round(sum(d["value"] for d in sorted_data), 1)
    return {
        "year":      year,
        "source":    source,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total":     total,
        "data":      sorted_data,
    }


def write_output(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info(f"Geschrieben: {path}  ({path.stat().st_size:,} Bytes)")


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Steuerspirale – Datenabruf von GENESIS-Online",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python fetch_data.py
      → aktuellstes Jahr auto-detect → data.json + data_YYYY.json

  python fetch_data.py --year 2023
      → GENESIS-Daten 2023 → data.json + data_2023.json

  python fetch_data.py --year latest
      → identisch zu ohne --year (auto-detect)

  python fetch_data.py --api-key KENNUNG:PASSWORT --year 2022
      → Key per Argument statt Umgebungsvariable

  python fetch_data.py --mock
      → nur Entwicklung: Mock-Daten ohne API-Key

Umgebungsvariablen:
  mein_genesis_key   API-Key im Format KENNUNG:PASSWORT
        """
    )
    parser.add_argument(
        "--year", type=str, default="latest",
        help="Berichtsjahr oder 'latest' für auto-detect (Standard: latest)"
    )
    parser.add_argument(
        "--api-key", type=str, default=None,
        help="GENESIS-API-Key (KENNUNG:PASSWORT). Alternativ: Env-Var mein_genesis_key"
    )
    parser.add_argument(
        "--output", type=str, default="data.json",
        help="Primäre Ausgabedatei (Standard: data.json)"
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="(Nur Entwicklung) Mock-Daten ohne API-Key verwenden"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Ausführliche Debug-Ausgabe"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output     = Path(args.output)
    api_key    = args.api_key or os.getenv("mein_genesis_key")
    force_mock = args.mock

    log.info("═" * 60)
    log.info("  STEUERSPIRALE — Datenabruf-Service")
    log.info("═" * 60)

    # ── Mock-Modus (nur für lokale Entwicklung) ───────────────────────
    if force_mock:
        log.warning("ACHTUNG: Mock-Modus aktiv — keine echten GENESIS-Daten!")
        year    = int(args.year) if args.year != "latest" else 2024
        data    = MOCK_DATA_2024
        source  = "mock"
        payload = build_output(data, year, source)
        write_output(payload, output)
        year_path = output.parent / f"data_{year}.json"
        write_output(payload, year_path)
        log.info("─" * 60)
        log.info(f"  [MOCK] Jahr={year}  Einträge={len(payload['data'])}  "
                 f"Gesamt={payload['total']:,.1f} Mio. €")
        return 0

    # ── Produktions-Modus: API-Key erforderlich ───────────────────────
    if not api_key:
        log.error("Kein API-Key gefunden!")
        log.error("Setze die Umgebungsvariable:  mein_genesis_key=KENNUNG:PASSWORT")
        log.error("Oder übergib:                 --api-key KENNUNG:PASSWORT")
        log.error("Für lokale Tests ohne Key:    --mock")
        sys.exit(1)

    # ── Jahres-Auflösung ──────────────────────────────────────────────
    if args.year == "latest":
        year, data = get_latest_year_with_data(api_key)
    else:
        year = int(args.year)
        data = fetch_year(api_key, year)
        if not data:
            log.error(f"Keine Daten für Jahr {year} von GENESIS erhalten. Abbruch.")
            sys.exit(1)

    source  = "genesis"
    payload = build_output(data, year, source)

    # ── Dateien schreiben ─────────────────────────────────────────────
    write_output(payload, output)                                      # data.json
    year_path = output.parent / f"data_{year}.json"
    write_output(payload, year_path)                                   # data_2024.json

    log.info("─" * 60)
    log.info(f"  Jahr:        {year}")
    log.info(f"  Steuerarten: {len(payload['data'])}")
    log.info(f"  Gesamt:      {payload['total']:,.1f} Mio. €")
    log.info(f"  Quelle:      {source.upper()}")
    log.info(f"  Zeitstempel: {payload['timestamp']}")
    log.info("─" * 60)
    log.info("✔ Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
