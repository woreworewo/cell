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
