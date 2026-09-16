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


"""
CLI Tool untuk mengimpor file data OpenCellID CSV ke database SQLite.

Mendukung:
  - File CSV spesifik negara (misal data/510.csv)
  - Dump CSV OpenCellID seluruh dunia (misal data/cell_towers_2026-*.csv)
  - Opsi filter MCC (misal --mcc 510 untuk menyaring hanya Indonesia dari dump dunia)

Penggunaan:
    # Impor seluruh dunia dari file dump
    python import_csv.py data/cell_towers_2026-09-16-T000000.csv

    # Impor hanya Indonesia (MCC 510) dari file dump dunia
    python import_csv.py data/cell_towers_2026-09-16-T000000.csv --mcc 510

    # Impor file default (data/510.csv atau dump pertama yang ditemukan)
    python import_csv.py
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

from db import DATA_DIR, DB_PATH, DEFAULT_CSV, get_db_stats, import_csv_to_sqlite


def find_default_csv() -> Path:
    """Cari file CSV yang paling cocok di folder data."""
    # Prioritaskan file cell_towers_*.csv jika ada
    dumps = sorted(glob.glob(str(DATA_DIR / "cell_towers_*.csv")), reverse=True)
    if dumps:
        return Path(dumps[0])
    if DEFAULT_CSV.exists():
        return DEFAULT_CSV
    return DEFAULT_CSV


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    detected_csv = find_default_csv()

    parser = argparse.ArgumentParser(
        description="Import OpenCellID CSV ke SQLite cells.db (Dukung Seluruh Dunia & Filter MCC)"
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=str(detected_csv),
        help=f"Path ke file CSV (default terdeteksi: {detected_csv.name})",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="Path ke database SQLite target (default: data/cells.db)",
    )
    parser.add_argument(
        "--mcc",
        type=str,
        default=None,
        help="Filter MCC tertentu (misal: 510 untuk Indonesia saja, atau '510,502'). Default: None (impor seluruh dunia)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50000,
        help="Ukuran batch commit per query (default: 50000)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    db_path = Path(args.db)

    if not csv_path.exists():
        sys.exit(f"Error: File CSV tidak ditemukan: {csv_path}")

    mcc_filter: list[int] | None = None
    if args.mcc:
        try:
            mcc_filter = [int(x.strip()) for x in args.mcc.split(",") if x.strip()]
        except ValueError:
            sys.exit(f"Error: Format --mcc tidak valid: '{args.mcc}'. Contoh: --mcc 510")

    file_size_mb = csv_path.stat().st_size / (1024 * 1024)
    file_size_gb = file_size_mb / 1024
    size_str = f"{file_size_gb:.2f} GB" if file_size_gb >= 1.0 else f"{file_size_mb:.2f} MB"

    print("=" * 60)
    print(" OPENCELLID CSV -> SQLITE IMPORTER")
    print("=" * 60)
    print(f" File CSV Source : {csv_path} ({size_str})")
    print(f" Target Database : {db_path}")
    if mcc_filter:
        print(f" Filter MCC      : {mcc_filter} (Hanya negara tertentu)")
    else:
        print(" Cakupan Data    : SELURUH DUNIA (Semua MCC)")
    print(f" Batch Size      : {args.batch_size:,} baris/commit")
    print("-" * 60)
    print(" Memulai proses impor... (silakan tunggu)")

    try:
        total = import_csv_to_sqlite(
            csv_path=csv_path,
            db_path=db_path,
            mcc_filter=mcc_filter,
            batch_size=args.batch_size,
        )
        print("\n" + "=" * 60)
        print(f" SUKSES: Berhasil mengimpor {total:,} baris cell tower!")
        print("=" * 60)
        stats = get_db_stats(db_path)
        print(f" Ukuran Database : {stats.get('size_mb')} MB")
        print(f" Total Cell      : {stats.get('total_cells'):,}")
        print(f" Rincian Radio   : {stats.get('by_radio')}")
        print("=" * 60)
    except KeyboardInterrupt:
        print("\n[!] Impor dibatalkan oleh user.")
    except Exception as e:
        sys.exit(f"\n[X] Gagal mengimpor: {e}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
