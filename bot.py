"""
Telegram bot wrapper untuk LTE cell lookup.

Konfigurasi via .env (lihat .env.example):
  TG_BOT_TOKEN          token bot (wajib)
  TG_RATE_LIMIT_SEC     jeda minimum antar request per user (default 300)
  TG_BOT_NAME           nama bot di /start (default "LTE Cell Lookup")
  TG_DEFAULT_MCC/MNC    nilai default kalau user tidak isi (default 510 / 10)
  TG_INCLUDE_*          toggle bagian output (lihat .env.example)
  UWL_TOKEN             token Unwired Labs (boleh banyak, dipisah koma)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
                      LinkPreviewOptions, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from cell_lookup import (Result, bearing_deg, best_serving_sector,
                         compass_label, estimate_azimuth, haversine_m,
                         load_env, map_links, parse_tokens, resolve,
                         sector_azimuths)
from db import DB_PATH, ensure_db, query_nearby_cells

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_env(Path(__file__).with_name(".env"))


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
BOT_NAME = os.environ.get("TG_BOT_NAME", "LTE Cell Lookup")
RATE_LIMIT = _env_int("TG_RATE_LIMIT_SEC", 3)
DEFAULT_MCC = _env_int("TG_DEFAULT_MCC", 510)
DEFAULT_MNC = _env_int("TG_DEFAULT_MNC", 10)
INCLUDE_LOCATION = _env_bool("TG_INCLUDE_LOCATION", True)
INCLUDE_ADDRESS = _env_bool("TG_INCLUDE_ADDRESS", True)
INCLUDE_MAP_BUTTONS = _env_bool("TG_INCLUDE_MAP_BUTTONS", True)
INCLUDE_PLUS_CODE = _env_bool("TG_INCLUDE_PLUS_CODE", True)
INCLUDE_AZIMUTH = _env_bool("TG_INCLUDE_AZIMUTH", True)

UWL_TOKENS = parse_tokens(os.environ.get("UWL_TOKEN", ""))
EXHAUSTED: set[str] = set()  # token UWL yang sudah kena limit di session ini

# Token OpenCellID dipisah dari UWL karena kuotanya berbeda (1.000 vs 100
# request/hari) — token yang habis di satu layanan bisa masih jalan di
# layanan lain, jadi set exhausted-nya tidak boleh digabung.
OCID_TOKENS = parse_tokens(os.environ.get("OCID_TOKEN", ""))
OCID_EXHAUSTED: set[str] = set()

# Alamat untuk /batch. Nominatim membatasi ~1 request/detik tanpa API key,
# jadi jumlah cell yang di-geocode dibatasi dan jedanya dipaksa di sini.
BATCH_GEOCODE_MAX = _env_int("TG_BATCH_GEOCODE_MAX", 5)
BATCH_ADDR_MAXLEN = _env_int("TG_BATCH_ADDR_MAXLEN", 70)

# Cache alamat, kunci = koordinat dibulatkan 4 desimal (~11 m). Tower yang
# sama sering di-lookup berulang, dan tanpa cache tiap /batch membayar
# ~1 detik Nominatim per cell.
GEO_CACHE_DIR = Path(__file__).with_name("cache") / "geo"
GEO_CACHE_TTL = 30 * 24 * 3600  # 30 hari, samakan dengan cache lookup

# Nominatim meminta maksimal 1 request/detik. Lock ini berlaku untuk
# seluruh proses, jadi /batch dan /nearby tidak bisa saling menabrak.
_GEO_LOCK = threading.Lock()
_GEO_LAST = 0.0
GEO_MIN_INTERVAL = 1.1  # sedikit di atas 1 detik untuk margin

# Per-user rate-limit (in-memory)
LAST_REQUEST: dict[int, float] = {}

# Tower terakhir yang dilookup per user, dipakai handler location user
# untuk menghitung bearing & sektor terdekat.
# Value: (mcc, mnc, enb, cid, lat, lon, ts)
LAST_TOWER: dict[int, tuple[int, int, int, int, float, float, float]] = {}
LAST_TOWER_TTL = 30 * 60  # 30 menit

# Lokasi terakhir yang dishare user: user_id -> (lat, lon, ts)
LAST_USER_LOCATION: dict[int, tuple[float, float, float]] = {}
# Setting radius nearby user: user_id -> radius_m
USER_NEARBY_RADIUS: dict[int, float] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# Silence library chatter (HTTP polling requests, etc.)
for noisy in ("httpx", "httpcore", "telegram.ext.Application",
              "telegram.ext.Updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_secs(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} detik"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} menit {s} detik" if s else f"{m} menit"
    h, m = divmod(m, 60)
    return f"{h} jam {m} menit"


def check_rate_limit(user_id: int) -> int:
    """Return 0 kalau lolos, atau sisa detik kalau masih harus tunggu."""
    now = time.time()
    last = LAST_REQUEST.get(user_id, 0.0)
    elapsed = now - last
    if elapsed >= RATE_LIMIT:
        return 0
    return int(RATE_LIMIT - elapsed)


def stamp_request(user_id: int) -> None:
    LAST_REQUEST[user_id] = time.time()


@dataclass
class CellQuery:
    query_type: str  # "enb_cid" atau "lac_ci"
    mcc: int = DEFAULT_MCC
    mnc: int = DEFAULT_MNC
    enb: int | None = None
    cid: int | None = None   # sektor antena (1-255)
    lac: int | None = None   # Location Area Code (2G/3G) atau TAC (4G)
    ci: int | None = None    # Cell ID (2G/3G) atau ECI (4G)
    radio: str | None = None


def _parse_int(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    try:
        if token.lower().startswith("0x"):
            return int(token, 16)
        if any(c in "abcdefABCDEF" for c in token) and all(c in "0123456789abcdefABCDEF" for c in token):
            return int(token, 16)
        if token.isdigit():
            return int(token, 10)
    except ValueError:
        return None
    return None


def parse_cell_item(s: str, force_lac: bool = False) -> CellQuery | None:
    s = s.strip()
    if not s:
        return None

    s_lower = s.lower()
    lac_match = re.search(r"(?:lac|tac)[:=\s]+(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", s_lower)
    ci_match = re.search(r"(?:ci|cid|eci)[:=\s]+(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", s_lower)
    enb_match = re.search(r"enb[:=\s]+(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", s_lower)
    mcc_match = re.search(r"mcc[:=\s]+(\d+)", s_lower)
    mnc_match = re.search(r"mnc[:=\s]+(\d+)", s_lower)

    mcc = int(mcc_match.group(1)) if mcc_match else DEFAULT_MCC
    mnc = int(mnc_match.group(1)) if mnc_match else DEFAULT_MNC

    if lac_match and ci_match:
        lac_val = _parse_int(lac_match.group(1))
        ci_val = _parse_int(ci_match.group(1))
        if lac_val is not None and ci_val is not None:
            return CellQuery(query_type="lac_ci", mcc=mcc, mnc=mnc, lac=lac_val, ci=ci_val)

    if enb_match and ci_match:
        enb_val = _parse_int(enb_match.group(1))
        cid_val = _parse_int(ci_match.group(1))
        if enb_val is not None and cid_val is not None:
            return CellQuery(query_type="enb_cid", mcc=mcc, mnc=mnc, enb=enb_val, cid=cid_val)

    # Bersihkan pemisah kecuali 0x
    clean = re.sub(r"[\/\,\:\;\_\-]+", " ", s)
    tokens = clean.split()
    nums = [_parse_int(t) for t in tokens if _parse_int(t) is not None]

    if not nums:
        return None

    if force_lac:
        if len(nums) == 4:
            return CellQuery(query_type="lac_ci", mcc=nums[0], mnc=nums[1], lac=nums[2], ci=nums[3])
        if len(nums) == 2:
            return CellQuery(query_type="lac_ci", mcc=DEFAULT_MCC, mnc=DEFAULT_MNC, lac=nums[0], ci=nums[1])
        return None

    # Smart detection:
    if len(nums) == 1:
        # Jika hanya 1 angka panjang (> 65535), anggap sebagai ECI LTE
        if nums[0] > 65535:
            enb = nums[0] // 256
            cid = nums[0] % 256
            return CellQuery(query_type="enb_cid", mcc=DEFAULT_MCC, mnc=DEFAULT_MNC,
                             enb=enb, cid=cid, ci=nums[0])
        return None

    if len(nums) == 2:
        v1, v2 = nums[0], nums[1]
        # Jika angka kedua > 255, tidak mungkin nomor sektor LTE (1-255).
        # Jadi pasti merupakan format LAC + CI (atau TAC + ECI).
        if v2 > 255:
            return CellQuery(query_type="lac_ci", mcc=DEFAULT_MCC, mnc=DEFAULT_MNC, lac=v1, ci=v2)
        else:
            return CellQuery(query_type="enb_cid", mcc=DEFAULT_MCC, mnc=DEFAULT_MNC, enb=v1, cid=v2)

    if len(nums) == 4:
        v1, v2, v3, v4 = nums[0], nums[1], nums[2], nums[3]
        if v4 > 255:
            return CellQuery(query_type="lac_ci", mcc=v1, mnc=v2, lac=v3, ci=v4)
        else:
            return CellQuery(query_type="enb_cid", mcc=v1, mnc=v2, enb=v3, cid=v4)

    return None


def parse_args(args: list[str], force_lac: bool = False) -> CellQuery | None:
    """Format diterima:
      /cell 510 10 11071 1
      /cell 11071 1
      /cell 18724 49384 (otomatis LAC+CI karena CI > 255)
      /cell lac 18724 49384
      /lac 18724 49384
    """
    if not args:
        return None
    joined = " ".join(args)
    if joined.lower().startswith("lac "):
        joined = joined[4:].strip()
        force_lac = True
    return parse_cell_item(joined, force_lac=force_lac)


def parse_batch_input(raw_text: str) -> list[CellQuery]:
    lines = raw_text.splitlines()
    if lines and lines[0].strip().startswith("/"):
        lines[0] = re.sub(r"^/\w+(@\w+)?", "", lines[0]).strip()

    cells: list[CellQuery] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        sub_items = [x.strip() for x in line.split(";") if x.strip()]
        for item in sub_items:
            parsed = parse_cell_item(item)
            if parsed:
                cells.append(parsed)
            else:
                tokens = [t for t in re.split(r"[\s,]+", item) if t]
                idx = 0
                while idx < len(tokens):
                    if idx + 4 <= len(tokens):
                        chunk = " ".join(tokens[idx:idx+4])
                        p = parse_cell_item(chunk)
                        if p:
                            cells.append(p)
                            idx += 4
                            continue
                    if idx + 2 <= len(tokens):
                        chunk = " ".join(tokens[idx:idx+2])
                        p = parse_cell_item(chunk)
                        if p:
                            cells.append(p)
                            idx += 2
                            continue
                    idx += 1
    return cells


def parse_radius(raw: str) -> float | None:
    s = raw.strip().lower()
    if not s:
        return None
    try:
        if s.endswith("km"):
            return float(s[:-2].strip()) * 1000.0
        if s.endswith("m"):
            return float(s[:-1].strip())
        val = float(s)
        if val <= 20:
            return val * 1000.0
        return val
    except ValueError:
        return None


def parse_nearby_args(args: list[str]) -> tuple[float | None, float | None, float]:
    default_radius = 1500.0
    if not args:
        return None, None, default_radius

    joined = " ".join(args).replace(",", " ")
    tokens = joined.split()

    if len(tokens) >= 2:
        try:
            lat = float(tokens[0])
            lon = float(tokens[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                rad = default_radius
                if len(tokens) >= 3:
                    parsed_rad = parse_radius(tokens[2])
                    if parsed_rad is not None:
                        rad = parsed_rad
                return lat, lon, max(100.0, min(rad, 15000.0))
        except ValueError:
            pass

    if len(tokens) == 1:
        rad = parse_radius(tokens[0])
        if rad is not None:
            return None, None, max(100.0, min(rad, 15000.0))

    return None, None, default_radius


def render_nearby(lat: float, lon: float, radius_m: float,
                  results: list[dict[str, Any]]) -> str:
    radius_km = radius_m / 1000.0
    lines = [
        f"<b>📡 Tower di Sekitar Lokasi (Radius {radius_km:.1f} km):</b>",
        f"📍 Titik pusat: <code>{lat:.5f}, {lon:.5f}</code>\n",
    ]

    if not results:
        lines.append(f"❌ Tidak ada tower ditemukan dalam radius {radius_km:.1f} km di database lokal.")
        lines.append("<i>Coba perbesar radius pencarian, contoh: <code>/nearby 3km</code></i>")
        return "\n".join(lines)

    for i, t in enumerate(results, 1):
        op = t["operator"] or f"MCC {t['mcc']}/{t['mnc']:02d}"
        dist_str = f"{t['distance_m']:.0f} m" if t['distance_m'] < 1000 else f"{t['distance_m']/1000:.2f} km"
        sectors_str = ", ".join(f"S{s}" for s in t["sectors"])
        tac_str = f" · TAC: <code>{t['area']}</code>" if t.get("area") else ""
        lines.append(
            f"<b>{i}. {op}</b> · eNB <code>{t['enb']}</code>\n"
            f"   • Jarak: <b>~{dist_str}</b> (Arah: {t['direction']}, {t['bearing']}°)\n"
            f"   • Sektor: <code>{sectors_str}</code>{tac_str}\n"
            f"   • 📍 <a href=\"https://www.google.com/maps?q={t['lat']},{t['lon']}\">{t['lat']}, {t['lon']}</a>"
        )
        lines.append("")

    lines.append(f"<i>Ditemukan {len(results)} tower terdekat dari database lokal.</i>")
    return "\n".join(lines)


def fmt_short_addr(r: Result) -> str:
    """Alamat satu baris untuk /batch, dipotong BATCH_ADDR_MAXLEN.

    Urutan field disesuaikan dengan keluaran Nominatim untuk Indonesia:
    `village` itu kelurahan dan `suburb` kecamatan, sedangkan kota
    (mis. "Jakarta Pusat") ada di `city_district` — `city` justru berisi
    nama provinsi ("Daerah Khusus Ibukota Jakarta"), jadi jangan dipakai
    lebih dulu.
    """
    addr = r.address_components or {}
    parts: list[str] = []

    def add(value: object) -> None:
        text = str(value).strip() if value else ""
        if text and text not in parts:
            parts.append(text)

    # Nama tempat (mal, kantor, pasar) paling informatif kalau ada.
    add(addr.get("amenity") or addr.get("building") or addr.get("shop"))
    add(addr.get("road"))
    for key in ("neighbourhood", "village", "suburb", "town"):
        if addr.get(key):
            add(addr[key])
            break
    for key in ("city_district", "city", "municipality", "county"):
        if addr.get(key):
            add(addr[key])
            break
    add(addr.get("country"))

    # Kalau kepanjangan, buang segmen paling belakang utuh daripada
    # memotong di tengah kata ("Jakarta Pusat, I…"). Dipotong keras hanya
    # kalau segmen pertama saja sudah melewati batas.
    while parts and len(", ".join(parts)) > BATCH_ADDR_MAXLEN:
        parts.pop()
    text = ", ".join(parts)
    if not text:
        text = re.sub(r"\s+", " ", r.display_name or "").strip()
    if not text:
        return ""
    if len(text) > BATCH_ADDR_MAXLEN:
        text = text[:BATCH_ADDR_MAXLEN - 1].rstrip() + "…"
    return text


def geocode_cached(lat: float, lon: float) -> dict:
    """Reverse geocode dengan cache + throttle global.

    Kembalikan dict Nominatim mentah; {} kalau gagal. Yang TIDAK di-cache
    hanya kegagalan transport/parse (reverse_geocode mengembalikan {}).
    Jawaban Nominatim tetap di-cache walau isinya error — mis. titik di
    tengah laut dibalas {"error": "Unable to geocode"} — karena itu
    jawaban final, bukan gangguan sesaat, dan menyimpannya menghemat
    kuota saat titik yang sama di-lookup lagi.
    """
    from cell_lookup import reverse_geocode

    key = f"{lat:.4f},{lon:.4f}"  # ~11 m, cukup khas per tower
    path = GEO_CACHE_DIR / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"
    if path.exists() and time.time() - path.stat().st_mtime <= GEO_CACHE_TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # rusak -> anggap tidak ada, ambil ulang

    global _GEO_LAST
    # Lock dipegang termasuk saat sleep: tanpa itu dua pemanggil
    # concurrent sama-sama melihat interval terpenuhi lalu menembak
    # Nominatim bersamaan.
    with _GEO_LOCK:
        gap = time.time() - _GEO_LAST
        if gap < GEO_MIN_INTERVAL:
            time.sleep(GEO_MIN_INTERVAL - gap)
        geo = reverse_geocode(lat, lon)
        _GEO_LAST = time.time()

    if not geo:
        return {}
    try:
        GEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("cache alamat gagal ditulis (%s): %s", path.name, e)
    return geo


def build_batch_txt(results: list[Result], geocoded: int) -> str:
    """Susun hasil /batch sebagai teks biasa (tanpa tag HTML).

    Format teks polos supaya enak dibaca di notepad dan tidak ada tag yang
    bocor kalau user membukanya di tempat lain. Yang di-geocode hanya
    `geocoded` cell pertama; sisanya cuma koordinat, sesuai batas
    TG_BATCH_GEOCODE_MAX.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    found = sum(1 for r in results if r.ok)
    if geocoded:
        addr_note = f"{geocoded} cell pertama (batas TG_BATCH_GEOCODE_MAX)"
    else:
        addr_note = "tidak diambil (TG_BATCH_GEOCODE_MAX=0 atau semua gagal)"
    out = [
        "LTE CELL LOOKUP — HASIL BATCH",
        f"Waktu    : {stamp}",
        f"Total    : {len(results)} cell ({found} ditemukan)",
        f"Alamat   : {addr_note}",
        "=" * 60,
        "",
    ]

    for i, r in enumerate(results, 1):
        op = r.operator or f"MCC {r.mcc}"
        out.append(f"[{i}] {op} — MCC/MNC {r.mcc}/{r.mnc:02d}")
        if r.radio.upper() == "LTE" and r.enb is not None:
            ident = f"eNB {r.enb} / Sektor {r.cid} (ECI {r.cid_full})"
            if r.lac is not None:
                ident += f" / TAC {r.lac}"
        else:
            ident = f"{r.radio} "
            if r.lac is not None:
                ident += f"LAC {r.lac} / "
            ident += f"CI {r.cid_full}"
        out.append(f"    Identitas : {ident}")

        if not r.ok:
            out.append(f"    Status    : TIDAK DITEMUKAN — {r.error}")
            out.append("")
            continue

        acc = f" (+/- {r.accuracy} m)" if r.accuracy is not None else ""
        out.append(f"    Koordinat : {r.lat}, {r.lon}{acc}")
        out.append(f"    Google Maps: https://www.google.com/maps?q={r.lat},{r.lon}")
        if INCLUDE_PLUS_CODE and r.plus_code:
            out.append(f"    Plus Code : {r.plus_code}")
        if INCLUDE_AZIMUTH and r.azimuth is not None:
            out.append(f"    Azimuth   : ~{r.azimuth:.0f} derajat "
                       f"({r.azimuth_label})")

        addr = fmt_short_addr(r)
        out.append(f"    Alamat    : {addr}" if addr
                   else "    Alamat    : (tidak tersedia)")

        src = {"local_db": "database lokal", "cache": "cache",
               "opencellid": "OpenCellID API"}.get(r.source, "Unwired Labs API")
        out.append(f"    Sumber    : {src}")
        out.append("")

    out.append("=" * 60)
    out.append(f"Berhasil: {found}/{len(results)} cell.")
    return "\n".join(out)


def render_text(r: Result) -> str:
    lines = [
        f"<b>📡 {r.country} — {r.operator}</b>",
        f"Radio: <b>{r.radio}</b> · MCC/MNC: <code>{r.mcc}/{r.mnc:02d}</code>",
    ]
    if r.radio.upper() == "LTE":
        enb_str = f"eNB: <code>{r.enb}</code> · sektor <code>{r.cid}</code>" if r.enb is not None else ""
        tac_str = f" · TAC: <code>{r.lac}</code>" if r.lac is not None else ""
        lines.append(f"{enb_str} (ECI <code>{r.cid_full}</code>){tac_str}".strip())
    else:
        lac_str = f"LAC: <code>{r.lac}</code> · " if r.lac is not None else ""
        lines.append(f"{lac_str}Cell ID: <code>{r.cid_full}</code>")

    if not r.ok:
        lines.append("")
        lines.append(f"❌ {r.error}")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"📍 <code>{r.lat}, {r.lon}</code>")
    if r.accuracy is not None:
        acc = f"± {r.accuracy} m"
        if r.fallback:
            acc += f" ({r.fallback})"
        lines.append(f"🎯 Akurasi: {acc}")
    if INCLUDE_PLUS_CODE and r.plus_code:
        lines.append(f"➕ Plus Code: <code>{r.plus_code}</code>")
    if INCLUDE_AZIMUTH and r.azimuth is not None:
        lines.append(
            f"🧭 Azimuth: ~{r.azimuth:.0f}° ({r.azimuth_label}) "
            f"<i>· estimasi, beamwidth ~{r.beamwidth:.0f}°</i>")

    if INCLUDE_ADDRESS:
        addr = r.address_components
        if addr:
            parts = []
            for k in ("road", "neighbourhood", "suburb", "village",
                     "town", "city", "state", "postcode", "country"):
                v = addr.get(k)
                if v:
                    parts.append(str(v))
            if parts:
                lines.append("")
                lines.append("🏠 " + ", ".join(parts))
        elif r.display_name:
            lines.append("")
            lines.append(f"🏠 {r.display_name}")

    if r.source == "local_db":
        lines.append("")
        lines.append("<i>📁 Sumber: Database Lokal (OpenCellID)</i>")
    elif r.from_cache or r.source == "cache":
        lines.append("")
        lines.append("<i>⚡ Sumber: Cache Lokal</i>")
    elif r.source == "opencellid":
        lines.append("")
        lines.append("<i>🌍 Sumber: OpenCellID API</i>")
    elif r.source == "unwiredlabs":
        lines.append("")
        lines.append("<i>🌐 Sumber: Unwired Labs API</i>")

    return "\n".join(lines)


def build_keyboard(r: Result) -> InlineKeyboardMarkup | None:
    if not (r.ok and INCLUDE_MAP_BUTTONS):
        return None
    rows = []
    row = []
    for name, url in map_links(r.lat, r.lon):
        row.append(InlineKeyboardButton(name, url=url))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
HELP_TEXT = (
    "<b>{name}</b>\n\n"
    "Lookup koordinat sektor seluler (4G LTE / 3G / 2G) dari database lokal & API.\n\n"
    "<b>Perintah Utama:</b>\n"
    "• <code>/cell &lt;enb&gt; &lt;cid&gt;</code> — lookup sektor 4G LTE\n"
    "• <code>/lac &lt;lac&gt; &lt;ci&gt;</code> — lookup via LAC dan CI (2G/3G/4G)\n"
    "• <code>/cell &lt;mcc&gt; &lt;mnc&gt; &lt;enb/lac&gt; &lt;cid/ci&gt;</code> — format lengkap\n"
    "• <code>/enb &lt;enb&gt;</code> — sweep semua sektor untuk 1 eNB LTE\n"
    "• <code>/batch</code> — lookup banyak cell sekaligus (maks 20), hasilnya dikirim sebagai file .txt\n"
    "• <code>/nearby [radius]</code> — cari tower terdekat dari lokasi Anda\n\n"
    "<b>Fitur Pintar:</b>\n"
    "• Mendukung desimal maupun heksadesimal (0x...).\n"
    "• Otomatis mendeteksi <code>eNB Sektor</code> vs <code>LAC CI</code> di /cell dan /batch.\n\n"
    "<b>Fitur Lokasi & Antena:</b>\n"
    "• Share lokasi (📎 → Location) setelah /cell untuk hitung jarak, arah bearing, dan estimasi sektor antena.\n"
    "• Share lokasi langsung tanpa /cell untuk melihat semua tower operator di sekitar Anda.\n\n"
    "Rate limit: 1 request per {rate} per user."
)


async def start_cmd(update: Update,
                    context: ContextTypes.DEFAULT_TYPE) -> None:
    text = HELP_TEXT.format(
        name=BOT_NAME, dmcc=DEFAULT_MCC, dmnc=DEFAULT_MNC,
        rate=fmt_secs(RATE_LIMIT),
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(
                                        is_disabled=True))


async def cell_cmd(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    parsed = parse_args(context.args or [])
    if parsed is None:
        await msg.reply_text(
            "Format salah. Contoh:\n"
            f"• 4G LTE: <code>/cell 11071 1</code>\n"
            f"• LAC + CI: <code>/cell 18724 49384</code> atau <code>/lac 18724 49384</code>\n"
            f"• Lengkap: <code>/cell {DEFAULT_MCC} {DEFAULT_MNC} 11071 1</code>",
            parse_mode=ParseMode.HTML)
        return

    wait = check_rate_limit(user.id)
    if wait > 0:
        await msg.reply_text(
            f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
        return

    if not UWL_TOKENS and not OCID_TOKENS and not DB_PATH.exists():
        await msg.reply_text(
            "⚠️ Bot belum dikonfigurasi (database lokal, OCID_TOKEN, dan "
            "UWL_TOKEN tidak tersedia).")
        return

    log.info("user=%s lookup q=%s", user.id, parsed)
    stamp_request(user.id)

    # Lookup di thread agar tidak block event loop
    result = await asyncio.to_thread(
        resolve,
        mcc=parsed.mcc,
        mnc=parsed.mnc,
        enb=parsed.enb,
        cid=parsed.cid,
        lac=parsed.lac,
        ci=parsed.ci,
        radio=parsed.radio,
        tokens=UWL_TOKENS,
        exhausted=EXHAUSTED,
        ocid_tokens=OCID_TOKENS,
        ocid_exhausted=OCID_EXHAUSTED,
        use_cache=True,
    )

    text = render_text(result)
    keyboard = build_keyboard(result)
    await msg.reply_text(text, parse_mode=ParseMode.HTML,
                         reply_markup=keyboard,
                         link_preview_options=LinkPreviewOptions(
                             is_disabled=True))

    if result.ok and INCLUDE_LOCATION:
        await msg.reply_location(latitude=result.lat, longitude=result.lon)

    if result.ok:
        LAST_TOWER[user.id] = (result.mcc, result.mnc, result.enb or 0, result.cid or 0,
                               result.lat, result.lon, time.time())


async def lac_cmd(update: Update,
                  context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lookup sel berdasarkan LAC dan CI (2G / 3G / 4G)."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    parsed = parse_args(context.args or [], force_lac=True)
    if parsed is None:
        await msg.reply_text(
            "Format salah. Contoh:\n"
            "• <code>/lac 18724 49384</code>\n"
            f"• <code>/lac {DEFAULT_MCC} {DEFAULT_MNC} 18724 49384</code>\n"
            "• Heksadesimal: <code>/lac 0x4924 0xC0E8</code>",
            parse_mode=ParseMode.HTML)
        return

    wait = check_rate_limit(user.id)
    if wait > 0:
        await msg.reply_text(
            f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
        return

    if not UWL_TOKENS and not OCID_TOKENS and not DB_PATH.exists():
        await msg.reply_text(
            "⚠️ Bot belum dikonfigurasi (database lokal, OCID_TOKEN, dan "
            "UWL_TOKEN tidak tersedia).")
        return

    log.info("user=%s lac lookup q=%s", user.id, parsed)
    stamp_request(user.id)

    result = await asyncio.to_thread(
        resolve,
        mcc=parsed.mcc,
        mnc=parsed.mnc,
        lac=parsed.lac,
        ci=parsed.ci,
        radio=parsed.radio,
        tokens=UWL_TOKENS,
        exhausted=EXHAUSTED,
        ocid_tokens=OCID_TOKENS,
        ocid_exhausted=OCID_EXHAUSTED,
        use_cache=True,
    )

    text = render_text(result)
    keyboard = build_keyboard(result)
    await msg.reply_text(text, parse_mode=ParseMode.HTML,
                         reply_markup=keyboard,
                         link_preview_options=LinkPreviewOptions(
                             is_disabled=True))

    if result.ok and INCLUDE_LOCATION:
        await msg.reply_location(latitude=result.lat, longitude=result.lon)

    if result.ok:
        LAST_TOWER[user.id] = (result.mcc, result.mnc, result.enb or 0, result.cid or 0,
                               result.lat, result.lon, time.time())


async def enb_cmd(update: Update,
                  context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lookup semua sektor untuk satu eNB sekaligus.

    Format:
      /enb <mcc> <mnc> <enb>
      /enb <enb>          (pakai default MCC/MNC)
      /enb <enb> <count>  (default count = 3)
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    args = list(context.args or [])
    # Pisahkan separator alternatif
    if len(args) == 1:
        for sep in ("-", "/", ",", "_"):
            if sep in args[0]:
                args = args[0].split(sep)
                break

    nums: list[int] = []
    for a in args:
        a = a.strip()
        if not a.lstrip("-").isdigit():
            await msg.reply_text("Format salah. Contoh: <code>/enb 11071</code>",
                                 parse_mode=ParseMode.HTML)
            return
        nums.append(int(a))

    if len(nums) == 1:
        mcc, mnc, enb = DEFAULT_MCC, DEFAULT_MNC, nums[0]
        count = len(sector_azimuths(mcc, mnc))
    elif len(nums) == 2:
        mcc, mnc, enb = DEFAULT_MCC, DEFAULT_MNC, nums[0]
        count = max(1, min(nums[1], 12))
    elif len(nums) == 3:
        mcc, mnc, enb = nums[0], nums[1], nums[2]
        count = len(sector_azimuths(mcc, mnc))
    elif len(nums) == 4:
        mcc, mnc, enb, count = nums[0], nums[1], nums[2], max(1, min(nums[3], 12))
    else:
        await msg.reply_text(
            "Format salah. Contoh:\n"
            f"<code>/enb 11071</code> (default {DEFAULT_MCC}/{DEFAULT_MNC}, "
            f"semua sektor)\n"
            "<code>/enb 510 10 11071</code>",
            parse_mode=ParseMode.HTML)
        return

    wait = check_rate_limit(user.id)
    if wait > 0:
        await msg.reply_text(
            f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
        return

    if not UWL_TOKENS and not OCID_TOKENS and not DB_PATH.exists():
        await msg.reply_text(
            "⚠️ Bot belum dikonfigurasi (database lokal, OCID_TOKEN, dan "
            "UWL_TOKEN tidak tersedia).")
        return

    log.info("user=%s sweep mcc=%s mnc=%s enb=%s count=%s",
             user.id, mcc, mnc, enb, count)
    stamp_request(user.id)

    results: list[Result] = []
    for sector in range(1, count + 1):
        # Argumen keyword, bukan posisional: urutan parameter resolve()
        # pernah bergeser saat lac/ci ditambahkan, dan versi posisional
        # mengirim lac=tokens, ci=exhausted, radio=True sehingga crash.
        r = await asyncio.to_thread(
            resolve, mcc=mcc, mnc=mnc, enb=enb, cid=sector,
            tokens=UWL_TOKENS, exhausted=EXHAUSTED,
            ocid_tokens=OCID_TOKENS, ocid_exhausted=OCID_EXHAUSTED,
            use_cache=True)
        results.append(r)

    ok_results = [r for r in results if r.ok]
    if not ok_results:
        await msg.reply_text(
            f"❌ Tidak ada sektor yang ditemukan untuk eNB <code>{enb}</code>.",
            parse_mode=ParseMode.HTML)
        return

    head = ok_results[0]
    lines = [
        f"<b>📡 eNB {enb} — {head.country} · {head.operator}</b>",
        f"MCC/MNC: <code>{head.mcc}/{head.mnc:02d}</code>",
        f"📍 <code>{head.lat}, {head.lon}</code>",
        "",
    ]
    for r in results:
        if r.ok:
            az = (f"~{r.azimuth:.0f}° ({r.azimuth_label})"
                  if r.azimuth is not None else "?")
            if r.source == "local_db":
                tag = " <i>(db lokal)</i>"
            elif r.from_cache or r.source == "cache":
                tag = " <i>(cache)</i>"
            elif r.source == "opencellid":
                tag = " <i>(OpenCellID)</i>"
            elif r.source == "unwiredlabs":
                tag = " <i>(online API)</i>"
            else:
                tag = ""
            lines.append(f"  • S{r.cid} → {az}{tag}")
        else:
            lines.append(f"  • S{r.cid} → ❌ {r.error}")

    keyboard = build_keyboard(head)
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                         reply_markup=keyboard,
                         link_preview_options=LinkPreviewOptions(
                             is_disabled=True))

    if INCLUDE_LOCATION:
        await msg.reply_location(latitude=head.lat, longitude=head.lon)

    LAST_TOWER[user.id] = (mcc, mnc, enb, head.cid,
                           head.lat, head.lon, time.time())


async def batch_cmd(update: Update,
                    context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lookup banyak cell tower sekaligus (maks 20 cell)."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    raw_text = msg.text or ""
    cells = parse_batch_input(raw_text)

    if not cells:
        await msg.reply_text(
            "<b>Format /batch:</b>\n"
            "Kirim daftar cell tower (satu per baris atau dipisah koma).\n\n"
            "<b>Mendukung Format:</b>\n"
            "• <code>&lt;enb&gt; &lt;cid&gt;</code> (4G LTE)\n"
            "• <code>&lt;lac&gt; &lt;ci&gt;</code> (2G/3G/4G, desimal atau heksa 0x...)\n"
            "• <code>&lt;mcc&gt; &lt;mnc&gt; &lt;enb/lac&gt; &lt;cid/ci&gt;</code> (Lengkap)\n\n"
            "<b>Contoh Multi-line:</b>\n"
            "<code>/batch\n"
            "11071 1\n"
            "18724 49384\n"
            "41004 104990981\n"
            "510 11 43003 4</code>\n\n"
            "<b>Contoh Satu Baris:</b>\n"
            "<code>/batch 11071:1, 18724:49384, 41004:104990981</code>\n\n"
            f"<i>Maks 20 cell per request.</i>",
            parse_mode=ParseMode.HTML
        )
        return

    MAX_BATCH = 20
    if len(cells) > MAX_BATCH:
        await msg.reply_text(
            f"⚠️ Maksimal {MAX_BATCH} cell per batch. Memproses {MAX_BATCH} cell pertama."
        )
        cells = cells[:MAX_BATCH]

    wait = check_rate_limit(user.id)
    if wait > 0:
        await msg.reply_text(f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
        return
    stamp_request(user.id)

    log.info("user=%s batch lookup %d cells", user.id, len(cells))

    # Resolve tanpa geocoding supaya cepat; alamat diurus terpisah di
    # bawah hanya untuk BATCH_GEOCODE_MAX cell pertama.
    results: list[Result] = []
    for q in cells:
        r = await asyncio.to_thread(
            resolve,
            mcc=q.mcc,
            mnc=q.mnc,
            enb=q.enb,
            cid=q.cid,
            lac=q.lac,
            ci=q.ci,
            radio=q.radio,
            tokens=UWL_TOKENS,
            exhausted=EXHAUSTED,
            ocid_tokens=OCID_TOKENS,
            ocid_exhausted=OCID_EXHAUSTED,
            use_cache=True,
            use_local_db=True,
            geocode=False,
        )
        results.append(r)

    found_count = sum(1 for r in results if r.ok)

    # Alamat: hanya untuk cell yang berhasil, dibatasi jumlahnya, dan
    # dengan jeda 1,1 detik supaya tidak kena HTTP 429 Nominatim.
    # Geocoding menahan respons ~5 detik, jadi kirim indikator typing
    # dulu supaya user tahu bot masih bekerja.
    geocodable = sum(1 for r in results
                     if r.ok and INCLUDE_ADDRESS and r.lat is not None)
    if geocodable:
        try:
            await msg.chat.send_action(ChatAction.TYPING)
        except Exception as e:  # bukan alasan menggagalkan hasil
            log.debug("gagal kirim typing action: %s", e)

    geocoded = 0
    for r in results:
        if geocoded >= BATCH_GEOCODE_MAX:
            break
        if not (r.ok and INCLUDE_ADDRESS and r.lat is not None):
            continue
        geo = await asyncio.to_thread(geocode_cached, r.lat, r.lon)
        if geo:
            r.address_components = geo.get("address") or {}
            r.display_name = geo.get("display_name") or r.display_name
        geocoded += 1

    # Hasil dikirim sebagai file .txt, bukan pesan: 20 cell + alamat bisa
    # lewat batas 4096 karakter Telegram, dan teks panjang lebih enak
    # dibaca dari editor.
    txt = build_batch_txt(results, geocoded)
    buf = io.BytesIO(txt.encode("utf-8"))
    buf.name = f"batch_{time.strftime('%Y%m%d-%H%M%S')}.txt"

    await msg.reply_document(
        document=InputFile(buf, filename=buf.name),
        caption=(f"📋 <b>Hasil batch: {found_count}/{len(results)} cell</b>\n"
                 f"<i>{geocoded} cell pertama disertai alamat.</i>"),
        parse_mode=ParseMode.HTML,
    )


async def nearby_cmd(update: Update,
                     context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cari cell tower terdekat dari lokasi user."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    lat, lon, radius_m = parse_nearby_args(context.args or [])
    USER_NEARBY_RADIUS[user.id] = radius_m

    # Jika koordinat diisi manual: /nearby <lat> <lon> [radius]
    if lat is not None and lon is not None:
        wait = check_rate_limit(user.id)
        if wait > 0:
            await msg.reply_text(f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
            return
        stamp_request(user.id)

        results = await asyncio.to_thread(query_nearby_cells, lat, lon, radius_m, 10)
        text = render_nearby(lat, lon, radius_m, results)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    # Jika ada lokasi user yang tersimpan dalam 30 menit terakhir
    last_loc = LAST_USER_LOCATION.get(user.id)
    if last_loc and time.time() - last_loc[2] < 1800:
        u_lat, u_lon, _ = last_loc
        results = await asyncio.to_thread(query_nearby_cells, u_lat, u_lon, radius_m, 10)
        text = render_nearby(u_lat, u_lon, radius_m, results)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    rad_km = radius_m / 1000.0
    await msg.reply_text(
        f"<b>📍 Cari Tower di Sekitar ({rad_km:.1f} km):</b>\n\n"
        "Silakan <b>share lokasi kamu</b> lewat tombol klip 📎 → <b>Location</b>.\n\n"
        "Atau ketik koordinat manual:\n"
        "• <code>/nearby &lt;lat&gt; &lt;lon&gt; [radius]</code>\n"
        "<i>Contoh: <code>/nearby -6.2088 106.8456 2km</code></i>",
        parse_mode=ParseMode.HTML
    )


async def location_handler(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kalau user share lokasi:
    1. Jika baru saja lookup tower (/cell atau /enb), hitung bearing & tebak sektor terdekat.
    2. Sediakan opsi cari tower sekitar, atau langsung cari jika belum lookup tower.
    """
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.location:
        return

    u_lat, u_lon = msg.location.latitude, msg.location.longitude
    LAST_USER_LOCATION[user.id] = (u_lat, u_lon, time.time())

    last = LAST_TOWER.get(user.id)
    # Jika ada tower yang baru dilookup (dalam 30 menit), hitung jarak & arah ke tower itu
    if last and time.time() - last[6] <= LAST_TOWER_TTL:
        mcc, mnc, enb, _, t_lat, t_lon, _ = last
        distance = haversine_m(t_lat, t_lon, u_lat, u_lon)
        bearing_t2u = bearing_deg(t_lat, t_lon, u_lat, u_lon)
        bearing_u2t = (bearing_t2u + 180) % 360
        sector, off_axis = best_serving_sector(mcc, mnc, bearing_t2u)

        if distance < 1000:
            dist_str = f"{distance:.0f} m"
        else:
            dist_str = f"{distance / 1000:.2f} km"

        quality = ("dalam pancar utama" if off_axis <= 32
                   else "agak miring dari pancar" if off_axis <= 60
                   else "di luar pancar utama")

        text = (
            f"<b>🧭 Posisi kamu vs tower eNB {enb}</b>\n"
            f"Jarak: <b>{dist_str}</b>\n"
            f"Tower → kamu: {bearing_t2u:.0f}° ({compass_label(bearing_t2u)})\n"
            f"Arah tower dari kamu: {bearing_u2t:.0f}° "
            f"({compass_label(bearing_u2t)})\n"
            f"\n"
            f"Sektor terdekat: <b>S{sector}</b> "
            f"(off-axis {off_axis:.0f}°, {quality})"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Cari Semua Tower di Sekitar Sini",
                                  callback_data=f"nearby:{u_lat:.5f}:{u_lon:.5f}")]
        ])
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    # Jika tidak ada tower sebelumnya, langsung lakukan radius search
    radius_m = USER_NEARBY_RADIUS.get(user.id, 1500.0)
    results = await asyncio.to_thread(query_nearby_cells, u_lat, u_lon, radius_m, 10)
    text = render_nearby(u_lat, u_lon, radius_m, results)
    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def callback_handler(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    if query.data.startswith("nearby:"):
        parts = query.data.split(":")
        if len(parts) == 3:
            try:
                lat, lon = float(parts[1]), float(parts[2])
                radius_m = USER_NEARBY_RADIUS.get(query.from_user.id, 1500.0)
                results = await asyncio.to_thread(
                    query_nearby_cells, lat, lon, radius_m, 10
                )
                text = render_nearby(lat, lon, radius_m, results)
                await query.message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except (ValueError, IndexError) as e:
                log.warning("Invalid callback data %s: %s", query.data, e)


async def fallback(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Pakai /cell untuk lookup, /batch untuk banyak cell, /nearby untuk sekitar, /start untuk bantuan.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TG_BOT_TOKEN belum diset di .env. Bikin bot lewat @BotFather "
            "lalu isi token-nya.")

    log.info("Starting bot '%s' (rate limit: %s, default MCC/MNC: %s/%s, "
             "tokens: %d)", BOT_NAME, fmt_secs(RATE_LIMIT),
             DEFAULT_MCC, DEFAULT_MNC, len(UWL_TOKENS))

    # Pastikan database lokal siap
    ensure_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("cell", cell_cmd))
    app.add_handler(CommandHandler("lac", lac_cmd))
    app.add_handler(CommandHandler("enb", enb_cmd))
    app.add_handler(CommandHandler("batch", batch_cmd))
    app.add_handler(CommandHandler("nearby", nearby_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.COMMAND, fallback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
