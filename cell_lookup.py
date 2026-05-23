"""
LTE Cell Lookup - core + CLI.

Ekspor fungsi `resolve()` untuk dipakai dari CLI maupun integrasi lain
(contoh: bot Telegram).

Fitur:
  - Decode operator MCC/MNC offline
  - Alamat terstruktur dari Nominatim
  - Plus Code (Open Location Code) dari koordinat (offline)
  - Link ke Google / OSM / Bing / Waze / Apple Maps
  - Akurasi + fallback flag dari Unwired Labs
  - Cache lokal 30 hari
  - Multi-token rotate otomatis
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

UWL_API = "https://ap1.unwiredlabs.com/v2/process.php"
NOMINATIM_API = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "cell-lookup-cli/1.2"
CACHE_DIR = Path(__file__).with_name("cache")
CACHE_TTL = 30 * 24 * 3600  # 30 hari


# ---------------------------------------------------------------------------
# Data MCC / MNC (subset)
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
    return MCC_COUNTRY.get(s_mcc, "?"), MNC_OPERATOR.get((s_mcc, s_mnc), "?")


# ---------------------------------------------------------------------------
# Plus Code
# ---------------------------------------------------------------------------
_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_RES = [20.0, 1.0, 0.05, 0.0025, 0.000125]


def plus_code(lat: float, lon: float) -> str:
    lat = max(-90.0, min(90.0, lat))
    lon = ((lon + 180) % 360) - 180
    if lat == 90:
        lat -= 0.000125
    a_lat, a_lon = lat + 90, lon + 180
    code = ""
    for i in range(5):
        place = _OLC_RES[i]
        d_lat = int(a_lat / place); a_lat -= d_lat * place
        d_lon = int(a_lon / place); a_lon -= d_lon * place
        code += _OLC_ALPHABET[d_lat] + _OLC_ALPHABET[d_lon]
        if i == 3:
            code += "+"
    return code


# ---------------------------------------------------------------------------
# Azimuth & geometri arah sinyal
# ---------------------------------------------------------------------------
# Mayoritas eNB LTE 3-sektor dengan antena 120° terpisah.
# Offset default mengikuti konvensi Telkomsel/Indosat di Indonesia:
#   sektor 1 -> 0° (Utara), 2 -> 120°, 3 -> 240°.
# Bisa di-override via env SECTOR_AZIMUTHS (CSV, urutan sektor 1..N) atau
# per-operator via SECTOR_AZIMUTHS_<MCC>_<MNC>.
DEFAULT_SECTOR_AZIMUTHS = (0.0, 120.0, 240.0)
DEFAULT_BEAMWIDTH_DEG = 65.0  # tipikal antena panel 3-sektor

_COMPASS_16 = (
    "Utara", "Utara-Timur Laut", "Timur Laut", "Timur-Timur Laut",
    "Timur", "Timur-Tenggara", "Tenggara", "Selatan-Tenggara",
    "Selatan", "Selatan-Barat Daya", "Barat Daya", "Barat-Barat Daya",
    "Barat", "Barat-Barat Laut", "Barat Laut", "Utara-Barat Laut",
)


def compass_label(deg: float) -> str:
    idx = int((deg % 360) / 22.5 + 0.5) % 16
    return _COMPASS_16[idx]


def _parse_azimuths(raw: str) -> tuple[float, ...] | None:
    if not raw:
        return None
    try:
        vals = tuple(float(x.strip()) % 360 for x in raw.split(",")
                     if x.strip())
        return vals or None
    except ValueError:
        return None


def sector_azimuths(mcc: int, mnc: int) -> tuple[float, ...]:
    """Resolve daftar azimuth sektor (urutan sektor 1..N), jatuh ke default."""
    key = f"SECTOR_AZIMUTHS_{mcc}_{mnc:02d}"
    for env_key in (key, "SECTOR_AZIMUTHS"):
        vals = _parse_azimuths(os.environ.get(env_key, ""))
        if vals:
            return vals
    return DEFAULT_SECTOR_AZIMUTHS


def estimate_azimuth(mcc: int, mnc: int, sector: int) -> float | None:
    """Perkiraan arah pancar antena untuk nomor sektor (1-based).
    Return None kalau sektor di luar tabel.
    """
    azimuths = sector_azimuths(mcc, mnc)
    if sector < 1 or sector > len(azimuths):
        return None
    return azimuths[sector - 1]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Jarak dua titik di permukaan bumi (meter)."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing initial dari titik 1 ke titik 2, derajat 0-360 (0 = Utara)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angular_diff(a: float, b: float) -> float:
    """Selisih sudut absolut terkecil antara dua bearing (0-180)."""
    d = abs((a - b + 180) % 360 - 180)
    return d


def best_serving_sector(mcc: int, mnc: int,
                        bearing_to_user: float) -> tuple[int, float]:
    """Cari sektor mana yang paling dekat arah pancarnya ke user.
    Return (sector_number, off_axis_deg).
    """
    azimuths = sector_azimuths(mcc, mnc)
    best_idx = 0
    best_diff = 360.0
    for i, az in enumerate(azimuths):
        d = angular_diff(az, bearing_to_user)
        if d < best_diff:
            best_diff = d
            best_idx = i
    return best_idx + 1, best_diff


# ---------------------------------------------------------------------------
# Env, helpers
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


def parse_tokens(raw: str) -> list[str]:
    out: list[str] = []
    for chunk in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        t = chunk.strip()
        if t and t not in out:
            out.append(t)
    return out


def map_links(lat: float, lon: float) -> list[tuple[str, str]]:
    return [
        ("Google", f"https://www.google.com/maps?q={lat},{lon}"),
        ("OSM",
         f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"),
        ("Bing", f"https://www.bing.com/maps?cp={lat}~{lon}&lvl=18"),
        ("Waze", f"https://www.waze.com/ul?ll={lat}%2C{lon}&navigate=yes"),
        ("Apple", f"https://maps.apple.com/?ll={lat},{lon}&z=18"),
    ]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def cache_path(mcc: int, mnc: int, enb: int, cid: int) -> Path:
    raw = f"lte:{mcc}:{mnc}:{enb}:{cid}".encode()
    return CACHE_DIR / f"{hashlib.sha1(raw).hexdigest()[:16]}.json"


def cache_get(path: Path) -> dict | None:
    if not path.exists() or time.time() - path.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def cache_put(path: Path, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as e:
        # Cache opsional - jangan gagalkan request kalau filesystem ngambek
        import logging
        logging.getLogger("cell_lookup").warning(
            "cache write failed (%s): %s", path.name, e)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _is_token_error(resp: dict) -> bool:
    msg = (resp.get("message") or "").lower()
    return any(s in msg for s in (
        "balance", "limit", "quota", "exhaust",
        "invalid token", "no token", "token not", "disabled",
    ))


def call_unwiredlabs(tokens: list[str], mcc: int, mnc: int, enb: int,
                    cid: int, exhausted: set[str]) -> dict:
    import logging
    log = logging.getLogger("cell_lookup")

    payload_base = {
        "radio": "lte", "mcc": mcc, "mnc": mnc,
        "cells": [{"cid": enb * 256 + cid}], "address": 1,
    }
    last = {"status": "error", "message": "Tidak ada token yang bisa dipakai."}

    for token in tokens:
        if token in exhausted:
            continue
        body = dict(payload_base, token=token)
        try:
            r = requests.post(UWL_API, json=body,
                              headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            resp = r.json()
        except requests.RequestException as e:
            last = {"status": "error", "message": f"Network error: {e}"}
            continue
        except ValueError:
            last = {"status": "error", "message": "Response bukan JSON."}
            continue

        last = resp
        if resp.get("status") == "ok":
            return resp
        if _is_token_error(resp):
            log.info("token ...%s exhausted: %s", token[-6:],
                     resp.get("message"))
            exhausted.add(token)
            continue
        return resp  # error data, jangan rotate

    return last


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
# High-level resolver
# ---------------------------------------------------------------------------
@dataclass
class Result:
    ok: bool
    mcc: int
    mnc: int
    enb: int
    cid: int
    country: str = ""
    operator: str = ""
    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    fallback: str | None = None
    plus_code: str = ""
    azimuth: float | None = None
    beamwidth: float = DEFAULT_BEAMWIDTH_DEG
    address_components: dict = field(default_factory=dict)
    display_name: str = ""
    from_cache: bool = False
    error: str = ""

    @property
    def cid_full(self) -> int:
        return self.enb * 256 + self.cid

    @property
    def azimuth_label(self) -> str:
        return compass_label(self.azimuth) if self.azimuth is not None else ""


def resolve(mcc: int, mnc: int, enb: int, cid: int,
            tokens: list[str], exhausted: set[str] | None = None,
            use_cache: bool = True) -> Result:
    """One-shot lookup; returns Result."""
    exhausted = exhausted if exhausted is not None else set()
    country, operator = operator_info(mcc, mnc)
    base = Result(ok=False, mcc=mcc, mnc=mnc, enb=enb, cid=cid,
                  country=country, operator=operator)

    cpath = cache_path(mcc, mnc, enb, cid)
    resp: dict[str, Any] | None = cache_get(cpath) if use_cache else None
    from_cache = resp is not None

    if resp is None:
        if not tokens:
            base.error = "UWL_TOKEN belum diset."
            return base
        resp = call_unwiredlabs(tokens, mcc, mnc, enb, cid, exhausted)
        if resp.get("status") == "ok" and use_cache:
            cache_put(cpath, resp)

    base.from_cache = from_cache
    if resp.get("status") != "ok":
        base.error = resp.get("message") or "Database tidak ditemukan."
        return base

    lat, lon = float(resp["lat"]), float(resp["lon"])
    base.ok = True
    base.lat, base.lon = lat, lon
    base.accuracy = resp.get("accuracy")
    base.fallback = resp.get("fallback")
    base.plus_code = plus_code(lat, lon)
    base.azimuth = estimate_azimuth(mcc, mnc, cid)

    geo = reverse_geocode(lat, lon)
    base.address_components = geo.get("address") or {}
    base.display_name = geo.get("display_name") or resp.get("address") or ""
    return base


# ---------------------------------------------------------------------------
# CLI presentation
# ---------------------------------------------------------------------------
def ask_int(label: str, default: int | None = None) -> int:
    hint = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{hint}: ").strip()
        if not raw and default is not None:
            return default
        if raw.lstrip("-").isdigit():
            return int(raw)
        print("  ! harus angka")


def print_result(r: Result) -> None:
    tag = " (cache)" if r.from_cache else ""
    print(f"\n--- Hasil{tag} ---")
    print(f"  Negara   : {r.country}")
    print(f"  Operator : {r.operator}")
    print(f"  MCC/MNC  : {r.mcc}/{r.mnc:02d}")
    print(f"  eNB      : {r.enb}  sektor {r.cid}  (CID {r.cid_full})")

    if not r.ok:
        print(f"  Status   : error")
        print(f"  Pesan    : {r.error}")
        return

    print(f"  Lat,Lon  : {r.lat}, {r.lon}")
    acc = f"± {r.accuracy} m" if r.accuracy is not None else "?"
    if r.fallback:
        acc += f"  (fallback: {r.fallback})"
    print(f"  Akurasi  : {acc}")
    print(f"  PlusCode : {r.plus_code}")
    if r.azimuth is not None:
        print(f"  Azimuth  : ~{r.azimuth:.0f}° ({r.azimuth_label}) "
              f"[estimasi, beamwidth ~{r.beamwidth:.0f}°]")

    if r.address_components:
        print("  Alamat   :")
        for k in ("road", "neighbourhood", "suburb", "village", "town",
                  "city", "county", "state", "postcode", "country"):
            v = r.address_components.get(k)
            if v:
                print(f"    - {k:11}: {v}")
    elif r.display_name:
        print(f"  Alamat   : {r.display_name}")

    print("  Map      :")
    for name, url in map_links(r.lat, r.lon):
        print(f"    - {name:7}: {url}")


def main() -> None:
    load_env(Path(__file__).with_name(".env"))

    p = argparse.ArgumentParser(description="LTE cell lookup")
    p.add_argument("--mcc", type=int)
    p.add_argument("--mnc", type=int)
    p.add_argument("--enb", type=int)
    p.add_argument("--cid", type=int)
    p.add_argument("--token", default=os.environ.get("UWL_TOKEN", ""),
                   help="Token UWL (boleh banyak, dipisah koma)")
    p.add_argument("--no-cache", action="store_true")
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
        mcc = args.mcc if args.mcc is not None else ask_int("MCC", 510)
        mnc = args.mnc if args.mnc is not None else ask_int("MNC", 10)
        enb = args.enb if args.enb is not None else ask_int("eNB")
        cid = args.cid if args.cid is not None else ask_int("CID", 1)

        result = resolve(mcc, mnc, enb, cid, tokens, exhausted,
                         use_cache=not args.no_cache)
        print_result(result)

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
