"""
Update harian database cell tower dari OpenCellID.

Alur:
  1. Unduh file negara (mis. MCC 510 = Indonesia) dari opencellid.org
  2. Ekstrak .gz menjadi CSV di data/
  3. Impor ulang ke SQLite secara atomic (cells.db lama diganti di akhir)
  4. VACUUM + ANALYZE supaya file rapat dan query plan segar

Hanya memakai pustaka standar (urllib, gzip, sqlite3) supaya bisa jalan
tanpa dependency tambahan.

Penggunaan:
    # Unduh + impor untuk Indonesia (MCC 510), otomatis pakai OCID_TOKEN
    python update_db.py

    # Negara lain
    python update_db.py --mcc 502

    # Token langsung di CLI
    python update_db.py --token pk.xxxxxxxx

    # Pakai CSV yang sudah ada (tanpa unduh) — untuk re-import lokal
    python update_db.py --csv data/510.csv

    # Backup cells.db sebelum diganti
    python update_db.py --backup

Exit code: 0 sukses, 1 gagal (aman dipakai untuk alerting cron).
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cell_lookup import load_env, parse_tokens
from db import DATA_DIR, DB_PATH, get_db_stats, import_csv_to_sqlite

log = logging.getLogger("cell_update")

OCID_DOWNLOAD_URL = "https://opencellid.org/ocid/downloads"
USER_AGENT = "cell-lookup-updater/1.0"
DOWNLOAD_TIMEOUT = 600  # detik
COPY_CHUNK = 1 << 20  # 1 MB


class UpdateError(RuntimeError):
    """Kesalahan yang bisa dijelaskan ke pengguna (bukan bug)."""


def mask_token(token: str) -> str:
    """Samarkan token agar tidak bocor ke log."""
    if len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def env_int(key: str, default: int) -> int:
    """Baca integer dari environment, fallback ke default kalau kosong/aneh."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s='%s' bukan angka, pakai default %d", key, raw, default)
        return default


def resolve_token(cli_token: str | None) -> str:
    """Ambil token OpenCellID dari CLI, OCID_TOKEN, lalu UWL_TOKEN.

    OpenCellID dioperasikan Unwired Labs, jadi token format `pk.` sering
    sama. OCID_TOKEN diprioritaskan supaya bisa dipisah kalau perlu.
    """
    if cli_token:
        return cli_token.strip()

    for key in ("OCID_TOKEN", "UWL_TOKEN"):
        tokens = parse_tokens(os.environ.get(key, ""))
        if tokens:
            log.info("Token diambil dari %s (%s)", key, mask_token(tokens[0]))
            return tokens[0]
    return ""


def download_country_gz(mcc: int, token: str, dest_gz: Path) -> Path:
    """Unduh file negara dari OpenCellID ke dest_gz.

    OpenCellID membalas JSON (bukan gzip) kalau token salah atau kuota
    habis, jadi isi file diverifikasi lewat magic bytes sebelum dipakai.
    """
    url = (f"{OCID_DOWNLOAD_URL}?token={quote(token)}"
           f"&type=mcc&file={mcc}.csv.gz")
    dest_gz.parent.mkdir(parents=True, exist_ok=True)
    part = dest_gz.with_name(dest_gz.name + ".part")

    log.info("Mengunduh MCC %d dari OpenCellID (token %s)...",
             mcc, mask_token(token))
    t0 = time.time()

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                open(part, "wb") as f:
            shutil.copyfileobj(resp, f, COPY_CHUNK)
    except HTTPError as e:
        part.unlink(missing_ok=True)
        raise UpdateError(
            f"Server menolak unduhan (HTTP {e.code}). Cek token/kuota."
        ) from e
    except URLError as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Gagal konek ke OpenCellID: {e.reason}") from e

    # Verifikasi isi benar-benar gzip, bukan pesan error JSON.
    with open(part, "rb") as f:
        magic = f.read(2)
    if magic != b"\x1f\x8b":
        try:
            body = part.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = "<tidak terbaca>"
        part.unlink(missing_ok=True)
        raise UpdateError(
            "Respons bukan file gzip — kemungkinan token invalid, kuota "
            f"habis, atau MCC {mcc} tidak tersedia.\nRespons server: "
            f"{body.strip()[:300]}"
        )

    part.replace(dest_gz)
    size_mb = dest_gz.stat().st_size / (1024 * 1024)
    log.info("Unduhan selesai: %s (%.2f MB, %.1f detik)",
             dest_gz.name, size_mb, time.time() - t0)
    return dest_gz


def gunzip_to_csv(src_gz: Path, dest_csv: Path) -> Path:
    """Ekstrak src_gz ke dest_csv secara atomic."""
    part = dest_csv.with_name(dest_csv.name + ".part")
    log.info("Mengekstrak %s -> %s...", src_gz.name, dest_csv.name)
    with gzip.open(src_gz, "rb") as fi, open(part, "wb") as fo:
        shutil.copyfileobj(fi, fo, COPY_CHUNK)
    part.replace(dest_csv)
    size_mb = dest_csv.stat().st_size / (1024 * 1024)
    log.info("Ekstrak selesai: %s (%.2f MB)", dest_csv.name, size_mb)
    return dest_csv


def vacuum_db(db_path: Path) -> bool:
    """Rapatkan file DB dan segarkan statistik query planner.

    VACUUM butuh akses eksklusif, jadi gagal kalau bot masih memegang
    koneksi. Ini bukan alasan menggagalkan seluruh update — data sudah
    terimpor dengan benar sebelum fungsi ini dipanggil — jadi kegagalan
    hanya dicatat sebagai peringatan.

    Return True kalau berhasil.
    """
    size_before = db_path.stat().st_size / (1024 * 1024)
    log.info("Menjalankan VACUUM + ANALYZE...")
    try:
        # isolation_level=None -> autocommit, VACUUM tidak boleh di
        # dalam transaksi.
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning(
            "VACUUM dilewati (%s). Data sudah terimpor; hentikan bot "
            "lalu jalankan VACUUM manual kalau ingin file lebih rapat.", e
        )
        return False

    size_after = db_path.stat().st_size / (1024 * 1024)
    log.info("VACUUM selesai: %.2f MB -> %.2f MB", size_before, size_after)
    return True


def run_update(mcc: int,
               token: str,
               csv_override: Path | None = None,
               gz_path: Path | None = None,
               keep_gz: bool = False,
               backup: bool = False,
               batch_size: int = 50000) -> int:
    """Unduh (opsional), impor ulang, lalu optimasi. Kembalikan jumlah baris."""
    gz_path = gz_path or DATA_DIR / f"cell_towers_{mcc}.csv.gz"
    csv_path = DATA_DIR / f"cell_towers_{mcc}.csv"

    if csv_override is not None:
        if not csv_override.exists():
            raise UpdateError(f"CSV tidak ditemukan: {csv_override}")
        csv_path = csv_override
        log.info("Memakai CSV yang ada: %s", csv_path)
    else:
        if not token:
            raise UpdateError(
                "Token OpenCellID belum ada. Isi OCID_TOKEN di .env, "
                "atau jalankan dengan --token pk.xxxxxxxx"
            )
        download_country_gz(mcc, token, gz_path)
        gunzip_to_csv(gz_path, csv_path)
    # Backup lama hanya kalau diminta — importer sudah atomic, dan
    # menyalin DB multi-GB tiap hari membuang disk percuma.
    if backup and DB_PATH.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = DB_PATH.with_name(f"{DB_PATH.name}.bak-{stamp}")
        log.info("Backup %s -> %s", DB_PATH.name, bak.name)
        shutil.copy2(DB_PATH, bak)

    log.info("Mengimpor %s ke %s (filter MCC: %d)...",
             csv_path.name, DB_PATH.name, mcc)
    t0 = time.time()
    total = import_csv_to_sqlite(csv_path, DB_PATH, mcc_filter=mcc,
                                 batch_size=batch_size)
    log.info("Impor selesai: %d baris dalam %.1f detik",
             total, time.time() - t0)

    # .gz hanya dihapus kalau kita yang mengunduhnya — jangan sentuh file
    # milik pengguna saat mode --csv.
    if csv_override is None and not keep_gz:
        gz_path.unlink(missing_ok=True)

    vacuum_db(DB_PATH)
    return total


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_env(Path(__file__).with_name(".env"))

    parser = argparse.ArgumentParser(
        description="Update harian database cell tower dari OpenCellID"
    )
    parser.add_argument("--mcc", type=int, default=env_int("OCID_MCC", 510),
                        help="MCC negara (default: 510 = Indonesia)")
    parser.add_argument("--token", default=None,
                        help="Token OpenCellID (default: OCID_TOKEN di .env)")
    parser.add_argument("--csv", default=None,
                        help="Pakai CSV lokal, lewati proses unduh")
    parser.add_argument("--gz", default=None,
                        help="Path simpan file .gz (default: data/cell_towers_<mcc>.csv.gz)")
    parser.add_argument("--keep-gz", action="store_true",
                        help="Jangan hapus file .gz setelah ekstrak")
    parser.add_argument("--backup", action="store_true",
                        help="Backup cells.db sebelum diganti")
    parser.add_argument("--batch-size", type=int, default=50000,
                        help="Baris per commit saat impor (default: 50000)")
    args = parser.parse_args()

    token = resolve_token(args.token)

    try:
        total = run_update(
            mcc=args.mcc,
            token=token,
            csv_override=Path(args.csv) if args.csv else None,
            gz_path=Path(args.gz) if args.gz else None,
            keep_gz=args.keep_gz,
            backup=args.backup,
            batch_size=args.batch_size,
        )
    except UpdateError as e:
        log.error("GAGAL: %s", e)
        sys.exit(1)
    except FileNotFoundError as e:
        log.error("GAGAL: %s", e)
        sys.exit(1)
    except Exception as e:
        log.exception("GAGAL tak terduga: %s", e)
        sys.exit(1)

    stats = get_db_stats(DB_PATH)
    log.info("=" * 52)
    log.info("UPDATE SELESAI")
    log.info("  Total cell : %s", f"{stats.get('total_cells', total):,}")
    log.info("  Ukuran DB  : %s MB", stats.get("size_mb"))
    log.info("  Per radio  : %s", stats.get("by_radio"))
    log.info("=" * 52)


if __name__ == "__main__":
    main()
