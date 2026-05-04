#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          STEUERSPIRALE — Daten-Abruf-Service                    ║
║  Statistisches Bundesamt · GENESIS-Online API · Tabelle 71211-0001 ║
╚══════════════════════════════════════════════════════════════════╝

Ruft Steuereinnahmen aus GENESIS-Online (Destatis) ab und erzeugt
JSON-Dateien für die D3.js-Steuerspirale.

AUTHENTIFIZIERUNG (laut GENESIS-Doku v5.1):
  Option A — Persönlicher Token (32 Zeichen, empfohlen):
    username = TOKEN  |  kein password nötig
  Option B — Nutzerkennung + Passwort:
    username = KENNUNG:PASSWORT

  Token aus dem GENESIS-Webinterface:
    https://www-genesis.destatis.de → Benutzerprofil → „Webservice-Schnittstelle (API)"

API-SPEZIFIKATION:
  Methode:      POST
  Endpunkt:     https://www-genesis.destatis.de/genesisWS/rest/2020/data/table
  Credentials:  HTTP-Header (username / password)
  Parameter:    Request-Body (application/x-www-form-urlencoded)
  Antwort:      JSON mit Object.Content = CSV-String

VERWENDUNG:
  python fetch_data.py                        # auto-detect aktuellstes Jahr
  python fetch_data.py --year 2023            # bestimmtes Jahr
  python fetch_data.py --mock                 # (nur Entwicklung) ohne API

UMGEBUNGSVARIABLE:
  mein_genesis_key   →  Token (32 Zeichen)  ODER  KENNUNG:PASSWORT
"""

import csv
import io
import json
import logging
import os
import sys
import argparse
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
GENESIS_BASE = "https://www-genesis.destatis.de/genesisWS/rest/2020"
GENESIS_TABLE = "71211-0001"

# Metadaten-Präfixe, die beim CSV-Parsing übersprungen werden
_CSV_SKIP_PREFIXES = (
    "genesis", "©", "stand:", "datenlizenz", "statistisches bundesamt",
    "kassenmäßige", "tabelle", "__________", "steuereinnahmen",
)

# Aggregat-Kategorien aus der GENESIS-Tabelle 71211-0001
# (Summenzeilen, die Einzelsteuern bereits enthalten → Doppelzählung vermeiden)
_AGGREGATE_NAMES = {
    "gemeinschaftsteuern",
    "bundessteuern",
    "landessteuern",
    "gemeindesteuern",
    "ländersteuern",
    "eu-steuern",
    "steuern insgesamt",
    "steuern vom einkommen",
    "steuern vom umsatz",
    "steuern vom vermögen",
    "verbrauchsteuern",
    "zölle und abschöpfungen",
}


def parse_credentials(key_str: str) -> tuple[str, Optional[str]]:
    """
    Gibt (username, password) zurück.
    - 32-Zeichen-Token → (token, None)   — kein Passwort nötig
    - KENNUNG:PASSWORT → (kennung, passwort)
    """
    key_str = key_str.strip()
    if len(key_str) == 32 and ":" not in key_str:
        log.info("Token-Authentifizierung erkannt (32 Zeichen, kein Passwort)")
        return key_str, None
    if ":" in key_str:
        username, password = key_str.split(":", 1)
        log.info("Kennung/Passwort-Authentifizierung erkannt")
        return username.strip(), password.strip()
    # Unbekanntes Format — als Token behandeln
    log.warning(f"Unbekanntes Key-Format (Länge {len(key_str)}), behandle als Token")
    return key_str, None


def _post(endpoint: str, credentials: tuple[str, Optional[str]], body: dict,
          timeout: int = 45) -> dict:
    """
    POST-Request laut GENESIS-Doku v5.1:
      - Credentials als HTTP-Header (username, optional password)
      - Parameter als Body (application/x-www-form-urlencoded)
    """
    username, password = credentials
    headers = {"username": username}
    if password:
        headers["password"] = password

    log.debug(f"POST {endpoint}")
    log.debug(f"Body: {body}")

    if HAS_REQUESTS:
        resp = requests.post(endpoint, headers=headers, data=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    # Fallback: urllib
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=encoded, method="POST",
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_genesis_csv(content: str, year: int) -> Optional[list[dict]]:
    """
    Parst den CSV-String aus Object.Content der GENESIS-Antwort.

    Das CSV enthält oben Metadaten und Spaltenköpfe (Semikolon-getrennt),
    dann Datenzeilen im Format:
        Steuerart-Name;Wert_in_Tsd_EUR[;weitere Spalten...]

    Werte kommen in Tausend € → Division durch 1000 → Mio. €
    """
    results = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Metadaten-Zeilen überspringen
        lower = line.lower()
        if any(lower.startswith(p) for p in _CSV_SKIP_PREFIXES):
            continue

        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue

        name = parts[0]
        if not name:
            continue

        # Sonderzeichen / Kennzeichen überspringen
        if name.lower() in ("", "-", "x", ".", "/") or name.startswith("_"):
            continue

        # Aggregat-Kategorien überspringen (enthalten Einzelsteuern bereits)
        if name.lower() in _AGGREGATE_NAMES:
            log.debug(f"Aggregat übersprungen: {name}")
            continue

        # Numerischen Wert suchen (letzte nicht-leere Spalte)
        raw_val = None
        for col in reversed(parts[1:]):
            col = col.strip()
            if col and col not in ("-", "x", ".", "/", ""):
                raw_val = col
                break

        if raw_val is None:
            continue

        try:
            # Deutsche Formatierung: "248.920,4" → float
            # oder einfach "248920" → float
            normalized = raw_val.replace(".", "").replace(",", ".")
            value_tsd = float(normalized)
            value_mio = round(value_tsd / 1000, 1)
            if value_mio > 0:
                results.append({"name": name, "value": value_mio})
        except (ValueError, TypeError):
            log.debug(f"Überspringe nicht-numerische Zeile: {raw_val!r} ('{name}')")
            continue

    # Umsatzsteuer + Einfuhrumsatzsteuer zusammenführen
    ust   = next((r for r in results if "umsatzsteuer" in r["name"].lower()
                  and "einfuhr" not in r["name"].lower()), None)
    eust  = next((r for r in results if "einfuhrumsatzsteuer" in r["name"].lower()), None)
    if ust and eust:
        combined = round(ust["value"] + eust["value"], 1)
        log.info(f"Umsatzsteuer ({ust['value']}) + Einfuhrumsatzsteuer ({eust['value']}) "
                 f"= {combined} Mio. €")
        ust["value"] = combined
        ust["name"]  = "Umsatzsteuer"
        results.remove(eust)

    if results:
        log.info(f"{len(results)} Steuerarten aus CSV geparst (Jahr {year}).")
    else:
        log.warning("Keine Daten aus CSV extrahiert.")

    return results or None


def check_genesis_status(raw: dict, endpoint_label: str) -> bool:
    """Prüft Status-Code in der GENESIS-Antwort. Gibt True zurück wenn OK."""
    status = raw.get("Status", {})
    code   = status.get("Code", -1)
    msg    = status.get("Content", "?")
    stype  = status.get("Type", "?")

    if code == 0:
        log.info(f"GENESIS [{endpoint_label}] Status OK: {msg}")
        return True

    # Code 22 = Warning (oft harmlos, Daten trotzdem vorhanden)
    if code == 22:
        log.warning(f"GENESIS [{endpoint_label}] Warnung (Code 22): {msg}")
        return True

    log.error(f"GENESIS [{endpoint_label}] Fehler (Code {code}, {stype}): {msg}")
    return False


def fetch_genesis_table(credentials: tuple[str, Optional[str]], year: int) \
        -> Optional[list[dict]]:
    """Primärer Endpunkt: POST /data/table"""
    endpoint = f"{GENESIS_BASE}/data/table"
    body = {
        "name":       GENESIS_TABLE,
        "area":       "all",
        "compress":   "false",
        "transpose":  "false",
        "startyear":  str(year),
        "endyear":    str(year),
        "language":   "de",
        "job":        "false",
    }
    log.info(f"GENESIS /data/table  Jahr={year}")
    try:
        raw = _post(endpoint, credentials, body)
        if not check_genesis_status(raw, "data/table"):
            return None
        content = raw.get("Object", {}).get("Content", "")
        if not content:
            log.warning("Object.Content ist leer.")
            return None
        return parse_genesis_csv(content, year)
    except Exception as exc:
        log.warning(f"/data/table Fehler: {exc}")
        return None


def fetch_genesis_timeseries(credentials: tuple[str, Optional[str]], year: int) \
        -> Optional[list[dict]]:
    """Fallback-Endpunkt: POST /data/timeseries"""
    endpoint = f"{GENESIS_BASE}/data/timeseries"
    body = {
        "name":      GENESIS_TABLE,
        "area":      "all",
        "compress":  "false",
        "transpose": "false",
        "startyear": str(year),
        "endyear":   str(year),
        "language":  "de",
        "job":       "false",
    }
    log.info(f"GENESIS /data/timeseries  Jahr={year}")
    try:
        raw = _post(endpoint, credentials, body)
        if not check_genesis_status(raw, "data/timeseries"):
            return None
        content = raw.get("Object", {}).get("Content", "")
        if not content:
            log.warning("Object.Content ist leer (timeseries).")
            return None
        return parse_genesis_csv(content, year)
    except Exception as exc:
        log.warning(f"/data/timeseries Fehler: {exc}")
        return None


def logincheck(credentials: tuple[str, Optional[str]]) -> bool:
    """
    Testet Verbindung und Credentials gegen GENESIS.
    logincheck-Antwort hat ein eigenes Format:
      {"Status": "Sie wurden erfolgreich an- und abgemeldet!", "Username": "..."}
    """
    endpoint = f"{GENESIS_BASE}/helloworld/logincheck"
    body = {"language": "de"}
    log.info("Teste Verbindung (logincheck) …")
    try:
        raw = _post(endpoint, credentials, body, timeout=15)
        log.debug(f"logincheck raw: {raw}")

        if not isinstance(raw, dict):
            log.warning(f"Unerwartete Antwort: {raw!r}")
            return False

        # Erfolgsformat: {"Status": "Sie wurden erfolgreich...", "Username": "..."}
        status_val = raw.get("Status", "")
        username   = raw.get("Username", "")
        if username or (isinstance(status_val, str) and "erfolgreich" in status_val.lower()):
            log.info(f"✔ Login OK — Benutzer: {username}")
            return True

        # Fehlerformat: {"Status": {"Code": N, "Content": "..."}}
        if isinstance(status_val, dict):
            code = status_val.get("Code", -1)
            msg  = status_val.get("Content", str(status_val))
            if code == 0:
                log.info(f"✔ Login OK: {msg}")
                return True
            log.error(f"Login fehlgeschlagen (Code {code}): {msg}")
            return False

        log.warning(f"Unbekannte Antwort: {raw}")
        return False
    except Exception as exc:
        log.error(f"logincheck Fehler: {exc}")
        return False


def fetch_year(credentials: tuple[str, Optional[str]], year: int) \
        -> Optional[list[dict]]:
    """Versucht beide Endpunkte; gibt None zurück wenn beide fehlschlagen."""
    data = fetch_genesis_table(credentials, year)
    if not data:
        log.info("Primärer Endpunkt lieferte keine Daten, versuche timeseries …")
        data = fetch_genesis_timeseries(credentials, year)
    return data


def get_latest_year_with_data(credentials: tuple[str, Optional[str]]) \
        -> tuple[int, list[dict]]:
    """
    Probiert das aktuelle und die beiden Vorjahre durch.
    Gibt (year, data) zurück oder beendet das Programm mit Exit-Code 1.
    """
    current = datetime.now().year
    for year in range(current, current - 3, -1):
        log.info(f"Prüfe verfügbare Daten für {year} …")
        data = fetch_year(credentials, year)
        if data:
            log.info(f"✔ Aktuellstes verfügbares Jahr mit Daten: {year}")
            return year, data
    log.error("Keine Daten für die letzten 3 Jahre gefunden. Abbruch.")
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
        description="Steuerspirale – Datenabruf von GENESIS-Online (POST-API v5.1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python fetch_data.py
      → aktuellstes Jahr auto-detect → data.json + data_YYYY.json

  python fetch_data.py --year 2023
      → GENESIS-Daten 2023 → data.json + data_2023.json

  python fetch_data.py --check
      → nur logincheck (Verbindung + Token testen)

  python fetch_data.py --mock
      → (nur Entwicklung) Mock-Daten ohne API-Key

Umgebungsvariablen:
  mein_genesis_key   Token (32 Zeichen) ODER KENNUNG:PASSWORT
        """
    )
    parser.add_argument("--year",    type=str, default="latest",
                        help="Berichtsjahr oder 'latest' (Standard: latest)")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Key per Argument statt Env-Var")
    parser.add_argument("--output",  type=str, default="data.json",
                        help="Primäre Ausgabedatei (Standard: data.json)")
    parser.add_argument("--mock",    action="store_true",
                        help="(Nur Entwicklung) Mock-Daten ohne API-Key")
    parser.add_argument("--check",   action="store_true",
                        help="Nur logincheck durchführen, keine Daten abrufen")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug-Ausgabe")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output     = Path(args.output)
    raw_key    = args.api_key or os.getenv("mein_genesis_key")
    force_mock = args.mock

    log.info("═" * 60)
    log.info("  STEUERSPIRALE — Datenabruf-Service (GENESIS API v5.1)")
    log.info("═" * 60)

    # ── Mock-Modus (nur für lokale Entwicklung) ───────────────────────
    if force_mock:
        log.warning("ACHTUNG: Mock-Modus — keine echten GENESIS-Daten!")
        year    = int(args.year) if args.year != "latest" else 2024
        payload = build_output(MOCK_DATA_2024, year, "mock")
        write_output(payload, output)
        write_output(payload, output.parent / f"data_{year}.json")
        log.info(f"[MOCK] {len(payload['data'])} Einträge, {payload['total']:,.1f} Mio. €")
        return 0

    # ── Produktions-Modus: API-Key erforderlich ───────────────────────
    if not raw_key:
        log.error("Kein API-Key gefunden!")
        log.error("  Umgebungsvariable setzen:  mein_genesis_key=<IHR_TOKEN>")
        log.error("  Oder per Argument:          --api-key <IHR_TOKEN>")
        log.error("  Für lokale Tests:           --mock")
        sys.exit(1)

    credentials = parse_credentials(raw_key)

    # ── Nur Verbindungstest ───────────────────────────────────────────
    if args.check:
        ok = logincheck(credentials)
        sys.exit(0 if ok else 1)

    # ── Logincheck vorab (gibt frühzeitig Feedback bei falschen Credentials)
    logincheck(credentials)

    # ── Jahres-Auflösung ──────────────────────────────────────────────
    if args.year == "latest":
        year, data = get_latest_year_with_data(credentials)
    else:
        year = int(args.year)
        data = fetch_year(credentials, year)
        if not data:
            log.error(f"Keine Daten für Jahr {year} erhalten. Abbruch.")
            sys.exit(1)

    payload = build_output(data, year, "genesis")

    # ── Dateien schreiben ─────────────────────────────────────────────
    write_output(payload, output)                                      # data.json
    write_output(payload, output.parent / f"data_{year}.json")        # data_2024.json

    log.info("─" * 60)
    log.info(f"  Jahr:        {year}")
    log.info(f"  Steuerarten: {len(payload['data'])}")
    log.info(f"  Gesamt:      {payload['total']:,.1f} Mio. €")
    log.info(f"  Quelle:      GENESIS-API")
    log.info(f"  Zeitstempel: {payload['timestamp']}")
    log.info("─" * 60)
    log.info("✔ Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
