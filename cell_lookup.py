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

from db import ensure_db, query_local_db, query_local_db_lac_ci

UWL_API = "https://ap1.unwiredlabs.com/v2/process.php"
OCID_API = "https://opencellid.org/cell/get"
NOMINATIM_API = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "cell-lookup-cli/1.2"
CACHE_DIR = Path(__file__).with_name("cache")
CACHE_TTL = 30 * 24 * 3600  # 30 hari
OCID_TIMEOUT = 20  # detik


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


def _env_flag(key: str, default: bool) -> bool:
    """Baca toggle boolean dari environment (1/true/yes/on)."""
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


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
def cache_path(mcc: int, mnc: int, enb_or_cid: int,
               cid_or_lac: int | None = None,
               lac: int | None = None,
               radio: str = "lte") -> Path:
    if lac is not None:
        raw = f"{radio.lower()}:{mcc}:{mnc}:{lac}:{enb_or_cid}".encode()
    elif cid_or_lac is not None and cid_or_lac <= 255:
        # eNB + sector LTE
        raw = f"lte:{mcc}:{mnc}:{enb_or_cid}:{cid_or_lac}".encode()
    else:
        raw = f"{radio.lower()}:{mcc}:{mnc}:{cid_or_lac or 0}:{enb_or_cid}".encode()
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


def call_unwiredlabs(tokens: list[str], mcc: int, mnc: int,
                     enb_or_cid: int, cid: int | None = None,
                     exhausted: set[str] | None = None,
                     lac: int | None = None,
                     radio: str = "lte",
                     cid_full: int | None = None) -> dict:
    import logging
    log = logging.getLogger("cell_lookup")
    exhausted = exhausted if exhausted is not None else set()

    if cid_full is not None:
        target_cid = cid_full
    elif cid is not None and cid <= 255:
        target_cid = enb_or_cid * 256 + cid
    else:
        target_cid = enb_or_cid

    cell_obj: dict[str, Any] = {"cid": target_cid}
    if lac is not None:
        cell_obj["lac"] = lac

    payload_base = {
        "radio": radio.lower(), "mcc": mcc, "mnc": mnc,
        "cells": [cell_obj], "address": 1,
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


def _normalize_ocid(resp: dict, mcc: int, mnc: int,
                    lac: int | None, ci: int,
                    radio: str | None) -> dict:
    """Samakan bentuk respons OpenCellID dengan respons internal lain.

    OpenCellID memakai `range` untuk perkiraan jangkauan (meter), setara
    `accuracy` di Unwired Labs dan kolom `range` di database lokal.
    """
    rng = resp.get("range")
    accuracy = int(rng) if isinstance(rng, (int, float)) and rng > 0 else None
    return {
        "status": "ok",
        "lat": resp["lat"],
        "lon": resp["lon"],
        "accuracy": accuracy,
        "radio": (resp.get("radio") or radio or "").upper() or None,
        "mcc": resp.get("mcc", mcc),
        "mnc": resp.get("mnc", mnc),
        "area": resp.get("lac", lac),
        "cell": resp.get("cellid", ci),
        "samples": resp.get("samples"),
        "changeable": resp.get("changeable"),
        "source": "opencellid",
    }


def call_opencellid(tokens: list[str], mcc: int, mnc: int,
                    lac: int | None, ci: int,
                    radio: str | None = None,
                    exhausted: set[str] | None = None) -> dict:
    """Lookup sel via OpenCellID API (`/cell/get`).

    Beda penting dari Unwired Labs: `lac` WAJIB di sini (LAC/TAC/network
    id). Kalau tidak ada, fungsi ini langsung mengembalikan error tanpa
    memanggil API — jadi tidak membuang kuota.

    Jatah 1.000 request/hari per key. Code 2 (key tidak dikenal) dan 7
    (kuota harian habis) menandai token exhausted lalu rotate ke token
    berikutnya; "cell not found" tidak, karena itu bukan soal token.

    Return dict bentuk internal: {"status": "ok", ...} atau
    {"status": "error", "message": ...}.
    """
    import logging
    log = logging.getLogger("cell_lookup")
    exhausted = exhausted if exhausted is not None else set()

    if lac is None:
        return {"status": "error",
                "message": "OpenCellID butuh LAC/TAC, tapi tidak tersedia."}

    params: dict[str, Any] = {
        "mcc": mcc, "mnc": mnc, "lac": lac, "cellid": ci, "format": "json",
    }
    if radio:
        params["radio"] = radio.upper()

    last = {"status": "error", "message": "Tidak ada token OpenCellID."}

    for token in tokens:
        if token in exhausted:
            continue
        try:
            r = requests.get(OCID_API, params=dict(params, key=token),
                             headers={"User-Agent": USER_AGENT},
                             timeout=OCID_TIMEOUT)
            resp = r.json()
        except requests.RequestException as e:
            last = {"status": "error", "message": f"Network error: {e}"}
            continue
        except ValueError:
            last = {"status": "error",
                    "message": f"Respons bukan JSON (HTTP {r.status_code})."}
            continue

        if not isinstance(resp, dict):
            last = {"status": "error", "message": "Format respons tak dikenal."}
            continue

        # Sukses: koordinat ada. Tidak ada field "code" pada respons sukses.
        if "lat" in resp and "lon" in resp:
            return _normalize_ocid(resp, mcc, mnc, lac, ci, radio)

        code = resp.get("code")
        msg = resp.get("error") or f"HTTP {r.status_code}"

        # Kuota habis / key invalid -> token ini tidak berguna, rotate.
        if code in (2, 7) or r.status_code in (401, 429):
            log.info("token ...%s OpenCellID ditolak (code %s): %s",
                     token[-6:], code, msg)
            exhausted.add(token)
            last = {"status": "error", "message": msg}
            continue

        # "Cell not found" (code 1) atau error lain: bukan soal token,
        # biarkan fallback berikutnya yang mencoba.
        return {"status": "error", "message": msg}

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
    enb: int | None = None
    cid: int | None = None
    lac: int | None = None
    ci: int | None = None
    radio: str = "LTE"
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
    source: str = ""  # "local_db", "cache", "opencellid", "unwiredlabs"
    error: str = ""

    @property
    def cid_full(self) -> int:
        if self.ci is not None:
            return self.ci
        if self.radio.upper() == "LTE" and self.enb is not None and self.cid is not None:
            return self.enb * 256 + self.cid
        return self.cid or 0

    @property
    def azimuth_label(self) -> str:
        return compass_label(self.azimuth) if self.azimuth is not None else ""


def resolve(mcc: int = 510,
            mnc: int = 10,
            enb: int | None = None,
            cid: int | None = None,
            lac: int | None = None,
            ci: int | None = None,
            radio: str | None = None,
            tokens: list[str] | None = None,
            exhausted: set[str] | None = None,
            use_cache: bool = True,
            use_local_db: bool = True,
            geocode: bool = True,
            ocid_tokens: list[str] | None = None,
            ocid_exhausted: set[str] | None = None,
            use_opencellid: bool | None = None) -> Result:
    """One-shot lookup; returns Result.

    Bisa dipanggil dengan:
      - enb & cid (format 4G LTE)
      - lac & ci (format 2G/3G/4G)
      - ci saja (ECI 4G)

    Urutan sumber: database lokal -> cache -> OpenCellID API ->
    Unwired Labs API. OpenCellID didahulukan karena kuotanya 1.000
    request/hari (10x Unwired Labs) dan sumbernya sama dengan database
    lokal, tapi butuh LAC/TAC — kalau tidak ada, lapisan ini dilewati.
    """
    tokens = tokens or []
    exhausted = exhausted if exhausted is not None else set()
    ocid_tokens = ocid_tokens or []
    ocid_exhausted = ocid_exhausted if ocid_exhausted is not None else set()
    if use_opencellid is None:
        use_opencellid = _env_flag("OCID_LOOKUP", True)

    if ci is None:
        if enb is not None and cid is not None:
            ci = enb * 256 + cid
            radio = radio or "LTE"
        else:
            return Result(ok=False, mcc=mcc, mnc=mnc,
                          error="Harus mengisi eNB & CID atau LAC & CI.")
    else:
        if radio is None:
            radio = "LTE" if ci > 65535 else "GSM"
        if radio.upper() == "LTE" and enb is None and cid is None:
            enb = ci // 256
            cid = ci % 256

    country, operator = operator_info(mcc, mnc)
    base = Result(ok=False, mcc=mcc, mnc=mnc, enb=enb, cid=cid,
                  lac=lac, ci=ci, radio=radio or "LTE",
                  country=country, operator=operator)

    resp: dict[str, Any] | None = None
    source = ""

    # 1. Cek database lokal SQLite (OpenCellID)
    if use_local_db:
        resp = query_local_db_lac_ci(mcc=mcc, mnc=mnc, lac=lac, ci=ci, radio=radio)
        if resp:
            source = "local_db"
            base.mcc = resp.get("mcc", base.mcc)
            base.mnc = resp.get("mnc", base.mnc)
            base.country, base.operator = operator_info(base.mcc, base.mnc)
            base.radio = resp.get("radio", base.radio)
            if resp.get("area") is not None:
                base.lac = resp["area"]
            if resp.get("cell") is not None:
                base.ci = resp["cell"]
            if base.radio.upper() == "LTE":
                base.enb = base.ci // 256
                base.cid = base.ci % 256

    # 2. Cek file cache jika tidak ada di database lokal
    cpath = cache_path(base.mcc, base.mnc, enb_or_cid=base.ci,
                       cid_or_lac=base.cid, lac=base.lac, radio=base.radio)
    if resp is None and use_cache:
        resp = cache_get(cpath)
        if resp:
            source = "cache"

    # 3. OpenCellID API — butuh LAC/TAC, jatah 1.000 request/hari.
    #    Didahulukan dari Unwired Labs karena kuotanya 10x lebih besar dan
    #    sumber datanya sama dengan database lokal, jadi hasilnya konsisten.
    ocid_error = ""
    if (resp is None and use_opencellid and ocid_tokens
            and base.lac is not None):
        ocid_resp = call_opencellid(tokens=ocid_tokens, mcc=base.mcc,
                                    mnc=base.mnc, lac=base.lac, ci=base.ci,
                                    radio=base.radio,
                                    exhausted=ocid_exhausted)
        if ocid_resp.get("status") == "ok":
            resp = ocid_resp
            source = "opencellid"
            if use_cache:
                cache_put(cpath, resp)
        else:
            ocid_error = ocid_resp.get("message") or ""

    # 4. Fallback terakhir: Unwired Labs API
    if resp is None:
        if not tokens:
            if ocid_error:
                base.error = (f"OpenCellID: {ocid_error} · "
                              "UWL_TOKEN belum diset.")
            elif base.lac is None and ocid_tokens:
                base.error = ("Cell tidak ada di database lokal. OpenCellID "
                              "butuh LAC/TAC · UWL_TOKEN belum diset.")
            else:
                base.error = ("Data tidak ditemukan di database lokal dan "
                              "UWL_TOKEN belum diset.")
            return base
        resp = call_unwiredlabs(tokens=tokens, mcc=base.mcc, mnc=base.mnc,
                                enb_or_cid=base.ci, lac=base.lac,
                                radio=base.radio, exhausted=exhausted)
        if resp.get("status") == "ok":
            source = "unwiredlabs"
            if use_cache:
                cache_put(cpath, resp)

    base.from_cache = (source == "cache")
    base.source = source
    if not resp or resp.get("status") != "ok":
        base.error = (resp.get("message") if resp else None) or "Database tidak ditemukan."
        return base

    lat, lon = float(resp["lat"]), float(resp["lon"])
    base.ok = True
    base.lat, base.lon = lat, lon
    base.accuracy = resp.get("accuracy")
    base.fallback = resp.get("fallback")
    base.plus_code = plus_code(lat, lon)
    if base.radio.upper() == "LTE" and base.cid is not None:
        base.azimuth = estimate_azimuth(base.mcc, base.mnc, base.cid)
    else:
        base.azimuth = None

    if geocode:
        geo = reverse_geocode(lat, lon)
        base.address_components = geo.get("address") or {}
        base.display_name = geo.get("display_name") or resp.get("address") or ""
    else:
        base.display_name = resp.get("address") or ""

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
    tag = ""
    if r.source == "local_db":
        tag = " (db lokal)"
    elif r.from_cache or r.source == "cache":
        tag = " (cache)"
    elif r.source == "opencellid":
        tag = " (OpenCellID)"
    elif r.source == "unwiredlabs":
        tag = " (online API)"

    print(f"\n--- Hasil{tag} ---")
    print(f"  Negara   : {r.country}")
    print(f"  Operator : {r.operator}")
    print(f"  Radio    : {r.radio}")
    print(f"  MCC/MNC  : {r.mcc}/{r.mnc:02d}")
    if r.radio.upper() == "LTE":
        enb_str = f"eNB {r.enb}  sektor {r.cid}" if r.enb is not None else ""
        tac_str = f"  TAC {r.lac}" if r.lac is not None else ""
        print(f"  Cell ID  : {enb_str}  (ECI {r.cid_full}){tac_str}".strip())
    else:
        lac_str = f"LAC {r.lac}  " if r.lac is not None else ""
        print(f"  Cell ID  : {lac_str}CI {r.cid_full}")

    if r.source:
        source_label = {
            "local_db": "Database Lokal (OpenCellID)",
            "cache": "Cache Lokal",
            "opencellid": "OpenCellID API",
            "unwiredlabs": "Unwired Labs API",
        }.get(r.source, r.source)
        print(f"  Sumber   : {source_label}")

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

    p = argparse.ArgumentParser(description="LTE / GSM Cell lookup")
    p.add_argument("--mcc", type=int)
    p.add_argument("--mnc", type=int)
    p.add_argument("--enb", type=int)
    p.add_argument("--cid", type=int)
    p.add_argument("--lac", type=int, help="Location Area Code / Tracking Area Code")
    p.add_argument("--ci", type=int, help="Cell ID (2G/3G) atau ECI (4G)")
    p.add_argument("--token", default=os.environ.get("UWL_TOKEN", ""),
                   help="Token UWL (boleh banyak, dipisah koma)")
    p.add_argument("--ocid-token", default=os.environ.get("OCID_TOKEN", ""),
                   help="Token OpenCellID (boleh banyak, dipisah koma)")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-db", action="store_true", help="Bypass database lokal SQLite")
    p.add_argument("--no-ocid", action="store_true", help="Bypass OpenCellID API")
    args = p.parse_args()

    ensure_db()

    tokens = parse_tokens(args.token)
    ocid_tokens = parse_tokens(args.ocid_token)
    if tokens:
        print(f"[config] {len(tokens)} token UWL siap dipakai")
    else:
        print("[config] UWL_TOKEN belum diset (tanpa fallback Unwired Labs)")
    if ocid_tokens:
        print(f"[config] {len(ocid_tokens)} token OpenCellID siap dipakai")
    else:
        print("[config] OCID_TOKEN belum diset (OpenCellID API dilewati)")

    exhausted: set[str] = set()
    ocid_exhausted: set[str] = set()
    common = dict(
        tokens=tokens, exhausted=exhausted,
        ocid_tokens=ocid_tokens, ocid_exhausted=ocid_exhausted,
        use_cache=not args.no_cache,
        use_local_db=not args.no_db,
        use_opencellid=not args.no_ocid,
    )
    one_shot = (
        (args.enb is not None and args.cid is not None) or
        (args.ci is not None)
    )

    while True:
        if tokens and len(exhausted) >= len(tokens):
            print("\n! Semua token UWL sudah kena limit.")
        if ocid_tokens and len(ocid_exhausted) >= len(ocid_tokens):
            print("\n! Semua token OpenCellID sudah kena limit.")

        mcc = args.mcc if args.mcc is not None else ask_int("MCC", 510)
        mnc = args.mnc if args.mnc is not None else ask_int("MNC", 10)

        if args.ci is not None or args.lac is not None:
            lac = args.lac if args.lac is not None else ask_int("LAC/TAC", 0)
            ci = args.ci if args.ci is not None else ask_int("CI")
            result = resolve(mcc=mcc, mnc=mnc, lac=lac or None, ci=ci,
                             **common)
        else:
            enb = args.enb if args.enb is not None else ask_int("eNB (atau 0 untuk LAC/CI)", 0)
            if enb == 0:
                lac = ask_int("LAC/TAC")
                ci = ask_int("CI")
                result = resolve(mcc=mcc, mnc=mnc, lac=lac, ci=ci, **common)
            else:
                cid = args.cid if args.cid is not None else ask_int("CID", 1)
                result = resolve(mcc=mcc, mnc=mnc, enb=enb, cid=cid, **common)

        print_result(result)

        if one_shot:
            break
        if input("\nLagi? (y/N): ").strip().lower() not in ("y", "ya", "yes"):
            break
        args.mcc = args.mnc = args.enb = args.cid = args.lac = args.ci = None


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
