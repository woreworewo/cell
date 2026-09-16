"""
CLI Tool untuk mengimpor file data OpenCellID CSV (misal data/510.csv) ke database SQLite.

Penggunaan:
    python import_csv.py
    python import_csv.py data/510.csv
    python import_csv.py data/510.csv --db data/cells.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db import DATA_DIR, DB_PATH, DEFAULT_CSV, get_db_stats, import_csv_to_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import OpenCellID CSV ke SQLite cells.db"
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=str(DEFAULT_CSV),
        help="Path ke file CSV (default: data/510.csv)",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="Path ke database SQLite target (default: data/cells.db)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    db_path = Path(args.db)

    if not csv_path.exists():
        sys.exit(f"Error: File CSV tidak ditemukan: {csv_path}")

    print(f"File CSV Source : {csv_path} ({csv_path.stat().st_size / (1024*1024):.2f} MB)")
    print(f"Target Database : {db_path}")
    print("Memproses impor data...")

    try:
        total = import_csv_to_sqlite(csv_path, db_path)
        print(f"\nBerhasil mengimpor {total:,} baris cell tower!")
        stats = get_db_stats(db_path)
        print(f"Ukuran database : {stats.get('size_mb')} MB")
        print(f"Rincian Radio   : {stats.get('by_radio')}")
    except Exception as e:
        sys.exit(f"Gagal mengimpor: {e}")


if __name__ == "__main__":
    main()
