"""
LTE Cell Lookup - detailed edition.

Fitur:
  - Decode operator MCC/MNC offline (no API)
  - Alamat terstruktur dari Nominatim (1 hit, dibagi per komponen)
  - Plus Code (Open Location Code) dari koordinat (offline)
  - Link ke Google / OSM / Bing / Waze / Apple Maps
  - Akurasi, fallback flag, dan sisa balance dari Unwired Labs
  - Cache lokal 30 hari supaya lookup berulang gratis

Pakai:
    python cell_lookup.py
    python cell_lookup.py --mcc 510 --mnc 10 --enb 11071 --cid 1
    python cell_lookup.py --no-cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

UWL_API = "https://ap1.unwiredlabs.com/v2/process.php"
NOMINATIM_API = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "cell-lookup-cli/1.1"
CACHE_DIR = Path(__file__).with_name("cache")
CACHE_TTL = 30 * 24 * 3600  # 30 hari


# ---------------------------------------------------------------------------
# MCC -> negara (subset; tambahkan sendiri kalau perlu)
# ---------------------------------------------------------------------------
MCC_COUNTRY = {
    "208": "France", "234": "United Kingdom", "262": "Germany",
    "310": "United States", "311": "United States", "312": "United States",
    "404": "India", "405": "India", "440": "Japan", "441": "Japan",
    "450": "South Korea", "452": "Vietnam", "454": "Hong Kong",
    "456": "Cambodia", "457": "Laos", "460": "China", "466": "Taiwan",
    "470": "Bangladesh", "502": "Malaysia", "505": "Australia",
    "510": "Indonesia", "515": "Philippines", "520": "Thailand",
    "525": "Singapore", "528": "Brunei", "530": "New Zealand",
    "655": "South Africa", "724": "Brazil",
}

# MNC operator (fokus Indonesia, paling umum)
MNC_OPERATOR = {
    ("510", "00"): "PSN",
    ("510", "01"): "Indosat Ooredoo Hutchison",
    ("510", "03"): "StarOne (Indosat)",
    ("510", "07"): "Telkomsel",
    ("510", "08"): "AXIS (XL)",
    ("510", "09"): "Smartfren",
    ("510", "10"): "Telkomsel",
    ("510", "11"): "XL Axiata",
    ("510", "20"): "Telkomsel",
    ("510", "21"): "Indosat (IM3)",
    ("510", "27"): "Net1 Indonesia",
    ("510", "28"): "Smartfren",
    ("510", "88"): "Indosat",
    ("510", "89"): "Tri (Hutchison 3)",
    ("510", "99"): "Esia",
    # contoh tambahan negara lain yang umum
    ("502", "12"): "Maxis (MY)",
    ("502", "13"): "Celcom (MY)",
    ("502", "16"): "DiGi (MY)",
    ("525", "01"): "Singtel (SG)",
    ("525", "02"): "StarHub (SG)",
    ("525", "03"): "M1 (SG)",
}


def operator_info(mcc: int, mnc: int) -> tuple[str, str]:
    s_mcc = str(mcc)
    s_mnc = f"{mnc:02d}"
    country = MCC_COUNTRY.get(s_mcc, "?")
    operator = MNC_OPERATOR.get((s_mcc, s_mnc), "?")
    return country, operator


# ---------------------------------------------------------------------------
# Plus Code (Open Location Code) - 10-char pair encoding (~14m precision)
# Ref: https://github.com/google/open-location-code
# ---------------------------------------------------------------------------
_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_RESOLUTIONS = [20.0, 1.0, 0.05, 0.0025, 0.000125]


def plus_code(lat: float, lon: float) -> str:
    lat = max(-90.0, min(90.0, lat))
    lon = ((lon + 180) % 360) - 180
    if lat == 90:
        lat -= 0.000125
    a_lat = lat + 90
    a_lon = lon + 180

    code = ""
    for i in range(5):
        place = _OLC_RESOLUTIONS[i]
        d_lat = int(a_lat / place)
        a_lat -= d_lat * place
        d_lon = int(a_lon / place)
        a_lon -= d_lon * place
        code += _OLC_ALPHABET[d_lat] + _OLC_ALPHABET[d_lon]
        if i == 3:  # setelah 8 char tambahkan separator
            code += "+"
    return code


# ---------------------------------------------------------------------------
# Env loader & helpers
# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(),
                                  v.strip().strip('"').strip("'"))


def ask_int(label: str, default: int | None = None) -> int:
    hint = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{hint}: ").strip()
        if not raw and default is not None:
            return default
        if raw.lstrip("-").isdigit():
            return int(raw)
        print("  ! harus angka")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def cache_path(mcc: int, mnc: int, enb: int, cid: int) -> Path:
    raw = f"lte:{mcc}:{mnc}:{enb}:{cid}".encode()
    return CACHE_DIR / f"{hashlib.sha1(raw).hexdigest()[:16]}.json"


def cache_get(path: Path) -> dict | None:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def cache_put(path: Path, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def _is_token_error(resp: dict) -> bool:
    """Error yang berkaitan dengan token (quota habis / invalid / disabled).
    Berbeda dengan error data (cell tidak ditemukan) yang tidak perlu rotate.
    """
    msg = (resp.get("message") or "").lower()
    return any(s in msg for s in (
        "balance", "limit", "quota", "exhaust",
        "invalid token", "no token", "token not", "disabled",
    ))


def lookup(tokens: list[str], mcc: int, mnc: int, enb: int,
           cid: int, exhausted: set[str]) -> tuple[dict, str | None]:
    """Coba semua token sampai dapat ok atau semua kena masalah token.

    Returns (response, token_yang_dipakai).
    """
    payload_base = {
        "radio": "lte",
        "mcc": mcc,
        "mnc": mnc,
        "cells": [{"cid": enb * 256 + cid}],
        "address": 1,
    }

    last = {"status": "error", "message": "Tidak ada token yang bisa dipakai."}
    for token in tokens:
        if token in exhausted:
            continue

        body = dict(payload_base, token=token)
        r = requests.post(UWL_API, json=body,
                          headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        resp = r.json()
        last = resp

        if resp.get("status") == "ok":
            return resp, token

        if _is_token_error(resp):
            reason = resp.get("message", "?")
            print(f"  ! token ...{token[-6:]} bermasalah ({reason}), "
                  f"rotate ke berikutnya")
            exhausted.add(token)
            continue

        # Error data (cell tidak ada, dll) - hentikan rotasi
        return resp, token

    return last, None


def reverse_geocode(lat: float, lon: float) -> dict:
    try:
        r = requests.get(NOMINATIM_API,
                         params={"format": "jsonv2", "lat": lat, "lon": lon,
                                 "addressdetails": 1, "zoom": 18},
                         headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return {}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def map_links(lat: float, lon: float) -> list[tuple[str, str]]:
    return [
        ("Google", f"https://www.google.com/maps?q={lat},{lon}"),
        ("OSM",
         f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"),
        ("Bing", f"https://www.bing.com/maps?cp={lat}~{lon}&lvl=18"),
        ("Waze", f"https://www.waze.com/ul?ll={lat}%2C{lon}&navigate=yes"),
        ("Apple", f"https://maps.apple.com/?ll={lat},{lon}&z=18"),
    ]


def show(inputs: dict, resp: dict, from_cache: bool) -> None:
    tag = " (cache)" if from_cache else ""
    print(f"\n--- Hasil{tag} ---")

    country, operator = operator_info(inputs["mcc"], inputs["mnc"])
    cid_full = inputs["enb"] * 256 + inputs["cid"]

    print(f"  Negara   : {country}")
    print(f"  Operator : {operator}")
    print(f"  MCC/MNC  : {inputs['mcc']}/{inputs['mnc']:02d}")
    print(f"  eNB      : {inputs['enb']}  sektor {inputs['cid']}  "
          f"(CID {cid_full})")

    if resp.get("status") != "ok":
        print(f"  Status   : {resp.get('status', 'error')}")
        print(f"  Pesan    : "
              f"{resp.get('message', 'Database tidak ditemukan.')}")
        return

    lat, lon = resp["lat"], resp["lon"]
    accuracy = resp.get("accuracy", "?")
    fallback = resp.get("fallback")

    print(f"  Lat,Lon  : {lat}, {lon}")
    acc_line = f"± {accuracy} m"
    if fallback:
        acc_line += f"  (fallback: {fallback})"
    print(f"  Akurasi  : {acc_line}")
    print(f"  PlusCode : {plus_code(lat, lon)}")

    # Reverse geocode (optional, gratis dari OSM)
    geo = reverse_geocode(lat, lon)
    addr = geo.get("address") or {}
    if addr:
        print("  Alamat   :")
        for k in ("road", "neighbourhood", "suburb", "village", "town",
                  "city", "county", "state", "postcode", "country"):
            v = addr.get(k)
            if v:
                print(f"    - {k:11}: {v}")
    elif resp.get("address"):
        print(f"  Alamat   : {resp['address']}")

    print("  Map      :")
    for name, url in map_links(lat, lon):
        print(f"    - {name:7}: {url}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_once(args: argparse.Namespace, tokens: list[str],
             exhausted: set[str]) -> None:
    inputs = {
        "mcc": args.mcc if args.mcc is not None else ask_int("MCC", 510),
        "mnc": args.mnc if args.mnc is not None else ask_int("MNC", 10),
        "enb": args.enb if args.enb is not None else ask_int("eNB"),
        "cid": args.cid if args.cid is not None else ask_int("CID", 1),
    }
    cpath = cache_path(**inputs)

    if not args.no_cache:
        cached = cache_get(cpath)
        if cached:
            show(inputs, cached, from_cache=True)
            return

    try:
        resp, _ = lookup(tokens, exhausted=exhausted, **inputs)
    except requests.RequestException as e:
        print(f"\n  ! error: {e}")
        return

    if resp.get("status") == "ok" and not args.no_cache:
        cache_put(cpath, resp)

    show(inputs, resp, from_cache=False)


def parse_tokens(raw: str) -> list[str]:
    """Pisah token berdasarkan koma / titik koma / whitespace."""
    out: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        t = chunk.strip()
        if t and t not in out:  # buang duplikat, jaga urutan
            out.append(t)
    return out


def main() -> None:
    load_env(Path(__file__).with_name(".env"))

    p = argparse.ArgumentParser(description="LTE cell lookup")
    p.add_argument("--mcc", type=int)
    p.add_argument("--mnc", type=int)
    p.add_argument("--enb", type=int)
    p.add_argument("--cid", type=int)
    p.add_argument("--token", default=os.environ.get("UWL_TOKEN", ""),
                   help="Token UWL. Boleh banyak, dipisah koma.")
    p.add_argument("--no-cache", action="store_true",
                   help="Abaikan cache, paksa hit API")
    args = p.parse_args()

    tokens = parse_tokens(args.token)
    if not tokens:
        sys.exit("ERROR: UWL_TOKEN belum diset (.env / env / --token).")
    print(f"[config] {len(tokens)} token siap dipakai")

    exhausted: set[str] = set()
    one_shot = all(v is not None
                   for v in (args.mcc, args.mnc, args.enb, args.cid))

    while True:
        if len(exhausted) >= len(tokens):
            print("\n! Semua token sudah kena limit. Berhenti.")
            break

        run_once(args, tokens, exhausted)
        if one_shot:
            break
        if input("\nLagi? (y/N): ").strip().lower() not in ("y", "ya", "yes"):
            break
        args.mcc = args.mnc = args.enb = args.cid = None


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
