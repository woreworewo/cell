"""
Modul database SQLite untuk cell lookup lokal (OpenCellID / 510.csv).

Menyediakan:
  - ensure_db(): Otomatis import CSV ke SQLite jika cells.db belum ada
  - query_local_db(): Query cepat berdasarkan MCC, MNC, eNB, CID
  - import_csv_to_sqlite(): Fungsi builder/importer batch
"""

from __future__ import annotations

import csv
import logging
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("cell_db")

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "cells.db"
DEFAULT_CSV = DATA_DIR / "510.csv"


def get_db_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def import_csv_to_sqlite(csv_path: Path = DEFAULT_CSV,
                         db_path: Path = DB_PATH) -> int:
    """Import file CSV (OpenCellID format) ke database SQLite."""
    if not csv_path.exists():
        raise FileNotFoundError(f"File CSV tidak ditemukan: {csv_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_db = db_path.with_suffix(".tmp")
    if temp_db.exists():
        temp_db.unlink()

    log.info("Memulai impor %s -> %s...", csv_path.name, db_path.name)
    t0 = time.time()

    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("PRAGMA journal_mode = MEMORY")

    cur.execute("""
    CREATE TABLE cells (
        radio TEXT,
        mcc INTEGER,
        net INTEGER,
        area INTEGER,
        cell INTEGER,
        unit INTEGER,
        lon REAL,
        lat REAL,
        range INTEGER,
        samples INTEGER,
        changeable INTEGER,
        created INTEGER,
        updated INTEGER,
        averageSignal INTEGER
    )
    """)

    batch: list[tuple[Any, ...]] = []
    count = 0

    with open(csv_path, mode="r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 9:
                continue
            # Lewati jika baris pertama adalah header
            if row[0].strip().lower() in ("radio", "type"):
                continue

            try:
                radio = row[0].strip().upper()
                mcc = int(row[1])
                net = int(row[2])
                area = int(row[3]) if row[3] else None
                cell = int(row[4])
                unit = int(row[5]) if len(row) > 5 and row[5] else None
                lon = float(row[6])
                lat = float(row[7])
                rng = int(float(row[8])) if len(row) > 8 and row[8] else None
                samples = int(row[9]) if len(row) > 9 and row[9] else None
                changeable = int(row[10]) if len(row) > 10 and row[10] else None
                created = int(row[11]) if len(row) > 11 and row[11] else None
                updated = int(row[12]) if len(row) > 12 and row[12] else None
                avg_sig = int(row[13]) if len(row) > 13 and row[13] else None

                batch.append((
                    radio, mcc, net, area, cell, unit, lon, lat, rng,
                    samples, changeable, created, updated, avg_sig
                ))
                count += 1

                if len(batch) >= 10000:
                    cur.executemany(
                        "INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch
                    )
                    batch.clear()
            except (ValueError, IndexError):
                continue

        if batch:
            cur.executemany(
                "INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            batch.clear()

    # Index untuk lookup cepat (MCC, MNC, Cell ID)
    cur.execute("CREATE INDEX idx_cells_lookup ON cells (mcc, net, cell)")
    # Index tambahan untuk lookup LTE/radio
    cur.execute("CREATE INDEX idx_cells_radio ON cells (radio)")
    # Index koordinat untuk radius search
    cur.execute("CREATE INDEX idx_cells_coords ON cells (lat, lon)")

    conn.commit()
    conn.close()

    # Rename file temporary ke file target secara atomic
    temp_db.replace(db_path)
    t1 = time.time()
    size_mb = db_path.stat().st_size / (1024 * 1024)
    log.info("Impor selesai: %d data dalam %.2f detik (ukuran DB: %.2f MB)",
             count, t1 - t0, size_mb)
    return count


def ensure_db(force: bool = False) -> bool:
    """Pastikan database cells.db ada dan siap digunakan.

    Jika belum ada dan file 510.csv tersedia, lakukan impor otomatis.
    """
    if DB_PATH.exists() and not force:
        # Pastikan index koordinat sudah ada
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cells_coords ON cells (lat, lon)"
                )
        except Exception:
            pass
        return True

    if not DEFAULT_CSV.exists():
        log.warning("Database %s belum ada dan CSV %s tidak ditemukan.",
                    DB_PATH.name, DEFAULT_CSV.name)
        return False

    try:
        import_csv_to_sqlite(DEFAULT_CSV, DB_PATH)
        return True
    except Exception as e:
        log.error("Gagal melakukan auto-init database: %s", e)
        return False


def query_local_db(mcc: int, mnc: int, enb: int, cid: int,
                   db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """Cari koordinat sektor LTE dari database lokal SQLite.

    Dalam LTE:
      cell (ECI) = enb * 256 + cid
    """
    if not db_path.exists():
        if not ensure_db():
            return None

    eci = enb * 256 + cid
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Prioritaskan baris dengan timestamp updated terbaru
            cur.execute(
                """
                SELECT lat, lon, range, radio, area, unit, updated
                FROM cells
                WHERE mcc = ? AND net = ? AND cell = ?
                ORDER BY updated DESC
                LIMIT 1
                """,
                (mcc, mnc, eci),
            )
            row = cur.fetchone()
            if not row:
                return None

            return {
                "status": "ok",
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "accuracy": int(row["range"]) if row["range"] is not None else None,
                "radio": row["radio"],
                "area": row["area"],
                "unit": row["unit"],
                "source": "local_db",
            }
    except Exception as e:
        log.warning("Gagal query database lokal: %s", e)
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_COMPASS_16 = (
    "Utara", "Utara-Timur Laut", "Timur Laut", "Timur-Timur Laut",
    "Timur", "Timur-Tenggara", "Tenggara", "Selatan-Tenggara",
    "Selatan", "Selatan-Barat Daya", "Barat Daya", "Barat-Barat Daya",
    "Barat", "Barat-Barat Laut", "Barat Laut", "Utara-Barat Laut",
)


def _compass_label(deg: float) -> str:
    idx = int((deg % 360) / 22.5 + 0.5) % 16
    return _COMPASS_16[idx]


def query_nearby_cells(
    lat: float,
    lon: float,
    radius_m: float = 1500.0,
    limit: int = 10,
    mcc: int | None = None,
    mnc: int | None = None,
    radio: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Cari tower di sekitar titik koordinat (radius_m meter).

    Mengelompokkan sektor LTE yang berada pada eNB yang sama menjadi satu site.
    Mengembalikan list tower terdekat diurutkan dari jarak terdekat.
    """
    if not db_path.exists():
        if not ensure_db():
            return []

    # Hitung bounding box kasar
    lat_delta = radius_m / 111139.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    lon_delta = radius_m / (111139.0 * cos_lat)

    min_lat, max_lat = lat - lat_delta, lat + lat_delta
    min_lon, max_lon = lon - lon_delta, lon + lon_delta

    clauses = ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
    params: list[Any] = [min_lat, max_lat, min_lon, max_lon]

    if mcc is not None:
        clauses.append("mcc = ?")
        params.append(mcc)
    if mnc is not None:
        clauses.append("net = ?")
        params.append(mnc)
    if radio is not None:
        clauses.append("radio = ?")
        params.append(radio.upper())

    where_sql = " AND ".join(clauses)
    query_sql = f"""
        SELECT radio, mcc, net, area, cell, unit, lon, lat, range, updated
        FROM cells
        WHERE {where_sql}
    """

    # Lazy import operator_info untuk cegah circular import
    try:
        from cell_lookup import operator_info
    except ImportError:
        operator_info = lambda mc, mn: ("Indonesia" if str(mc) == "510" else "?", "?")

    sites: dict[tuple, dict[str, Any]] = {}

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query_sql, params)
            rows = cur.fetchall()

            for row in rows:
                r_lat = float(row["lat"])
                r_lon = float(row["lon"])
                dist = _haversine_m(lat, lon, r_lat, r_lon)
                if dist > radius_m:
                    continue

                r_mcc = int(row["mcc"])
                r_mnc = int(row["net"])
                r_radio = row["radio"]
                r_cell = int(row["cell"])
                r_area = row["area"]

                if r_radio == "LTE":
                    enb = r_cell // 256
                    cid = r_cell % 256
                    site_key = ("LTE", r_mcc, r_mnc, enb)
                else:
                    enb = r_cell
                    cid = r_cell
                    site_key = (r_radio, r_mcc, r_mnc, r_cell)

                if site_key not in sites:
                    bearing = _bearing_deg(lat, lon, r_lat, r_lon)
                    country, operator = operator_info(r_mcc, r_mnc)
                    sites[site_key] = {
                        "radio": r_radio,
                        "mcc": r_mcc,
                        "mnc": r_mnc,
                        "enb": enb,
                        "country": country,
                        "operator": operator,
                        "area": r_area,
                        "lat": r_lat,
                        "lon": r_lon,
                        "distance_m": dist,
                        "bearing": round(bearing, 1),
                        "direction": _compass_label(bearing),
                        "sectors": {cid} if r_radio == "LTE" else {r_cell},
                        "accuracy": row["range"],
                    }
                else:
                    curr = sites[site_key]
                    if r_radio == "LTE":
                        curr["sectors"].add(cid)
                    if dist < curr["distance_m"]:
                        curr["distance_m"] = dist
                        curr["lat"] = r_lat
                        curr["lon"] = r_lon
                        b = _bearing_deg(lat, lon, r_lat, r_lon)
                        curr["bearing"] = round(b, 1)
                        curr["direction"] = _compass_label(b)

    except Exception as e:
        log.warning("Gagal radius search database lokal: %s", e)
        return []

    results = list(sites.values())
    for item in results:
        item["sectors"] = sorted(list(item["sectors"]))
        item["distance_m"] = round(item["distance_m"], 1)

    results.sort(key=lambda x: x["distance_m"])
    return results[:limit]


def get_db_stats(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Mengambil ringkasan statistik database lokal."""
    if not db_path.exists():
        return {"exists": False}

    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM cells")
            total = cur.fetchone()[0]

            cur.execute("SELECT radio, COUNT(*) FROM cells GROUP BY radio")
            by_radio = dict(cur.fetchall())

            return {
                "exists": True,
                "path": str(db_path),
                "size_mb": round(db_path.stat().st_size / (1024 * 1024), 2),
                "total_cells": total,
                "by_radio": by_radio,
            }
    except Exception as e:
        return {"exists": True, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    print(f"Directory Data : {DATA_DIR}")
    print(f"Target DB      : {DB_PATH}")
    print(f"Source CSV     : {DEFAULT_CSV}")

    if not DB_PATH.exists():
        print("Database belum ada, menjalankan impor...")
        ensure_db()
    else:
        print("Database sudah ada:")

    stats = get_db_stats()
    print(f"Statistik      : {stats}")
