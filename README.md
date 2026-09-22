# LTE Cell Lookup

Lookup koordinat sektor 4G LTE dari MCC/MNC/eNB/CID, plus alamat lengkap
dan banyak metadata. Tidak ada perantara, langsung panggil API:

- `ap1.unwiredlabs.com` untuk koordinat
- `nominatim.openstreetmap.org` untuk alamat terstruktur

## Setup

```cmd
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`, isi token gratis dari https://unwiredlabs.com (100 lookup/hari).

## Pakai

Interaktif:
```cmd
python cell_lookup.py
```

One-shot:
```cmd
python cell_lookup.py --mcc 510 --mnc 10 --enb 11071 --cid 1
```

## Multi-token (auto-rotate)

Token Unwired Labs gratis dibatasi 100 lookup/hari per akun. Daftar beberapa
akun, lalu isi semua token-nya di `.env` dipisah koma:

```
UWL_TOKEN=pk.aaa...,pk.bbb...,pk.ccc...
```

Script akan rotate otomatis: kalau token pertama kena pesan "balance over" /
"limit" / "invalid", langsung pindah ke token berikutnya pada request yang sama.

Bisa juga lewat CLI:
```cmd
python cell_lookup.py --token pk.aaa,pk.bbb,pk.ccc
```

## Bypass cache

Paksa refresh (hit API meski ada cache):
```cmd
python cell_lookup.py --no-cache
```

## Output

- Negara dan operator (decoded offline dari MCC/MNC)
- Koordinat lat/lon dengan akurasi asli dari Unwired Labs
- Plus Code (Open Location Code) - lokasi pendek tanpa URL
- Estimasi azimuth sektor (arah pancar antena) + label kompas
- Alamat terstruktur: jalan, kecamatan, kota, kode pos
- Link ke Google Maps, OpenStreetMap, Bing, Waze, Apple Maps
- Fallback flag (`cidf` = exact CID, `lacf` = approximate via LAC)

### Estimasi azimuth

Mayoritas eNB LTE 3-sektor dengan antena terpisah 120°. Default:
sektor 1 = 0° (Utara), 2 = 120° (Tenggara), 3 = 240° (Barat Daya).
Override lewat `.env`:

```
# Global
SECTOR_AZIMUTHS=30,150,270

# Atau per operator (MNC selalu 2 digit)
SECTOR_AZIMUTHS_510_10=0,120,240   # Telkomsel
SECTOR_AZIMUTHS_510_11=30,150,270  # XL
```

Nilai ini estimasi karena setiap site bisa di-tilt sesuai topografi.

## Cache

Hasil sukses disimpan di `cache/*.json` selama 30 hari. Lookup berulang
tidak menghabiskan kuota. Cache dimatikan untuk satu run dengan `--no-cache`,
atau hapus folder `cache/` untuk reset total.

## Bot Telegram

Wrapper bot pakai `bot.py`. Siapapun yang tahu username bot bisa pakai,
dengan rate limit per user (default 5 menit).

### Setup

1. Bikin bot di [@BotFather](https://t.me/BotFather), copy token-nya.
2. Tambah ke `.env`:

   ```
   TG_BOT_TOKEN=123456:ABC-DEF...
   TG_RATE_LIMIT_SEC=300
   TG_DEFAULT_MCC=510
   TG_DEFAULT_MNC=10
   ```

3. Install dependency dan jalankan:

   ```cmd
   pip install -r requirements.txt
   python bot.py
   ```

### Pakai

Di chat dengan bot:

- `/cell 11071 1` - lookup 4G LTE via eNB & Sektor (default MCC/MNC dari `.env`)
- `/cell 510 10 11071 1` - format 4G LTE lengkap
- `/lac 18724 49384` - lookup via **LAC & CI** (2G / 3G / 4G)
- `/cell 18724 49384` - otomatis dideteksi sebagai LAC+CI karena CI > 255
- `/cell 0x4924 0xC0E8` - mendukung format heksadesimal dari modem / AT Command
- `/enb 11071` - sweep semua sektor sekaligus untuk 1 eNB LTE, list azimuth tiap sektor
- `/batch` - lookup banyak cell sekaligus (maks 20 cell, bisa kombinasi eNB+CID maupun LAC+CI)
- `/nearby [radius]` - cari semua tower di sekitar koordinat/lokasi Anda
- `/start` atau `/help` - bantuan

**Fitur Pintar Deteksi Format:**
- Input 2 angka kecil (`11071 1`): Dikenali sebagai `eNB Sektor` (4G LTE).
- Input 2 angka besar (`18724 49384` atau `41004 104990981`): Otomatis dikenali sebagai `LAC CI` atau `TAC ECI`.
- Input heksadesimal (`0x...` atau `4924 C0E8`): Otomatis dikonversi ke desimal.
- Input kata kunci: `lac:18724 ci:49384` atau `tac:41004 eci:104990981`.

**Fitur Lokasi & Antena:**
- Setelah `/cell` atau `/enb` sukses, share lokasi kamu lewat klip 📎 → Location. Bot akan balas dengan jarak ke tower, bearing arah tower, dan tebakan sektor mana yang seharusnya melayani posisi kamu.
- Share lokasi langsung tanpa `/cell` untuk menampilkan semua tower BTS dari berbagai operator di sekitar radius kamu (default 1.5 km).

Bot membalas dengan:
- Info operator + koordinat + akurasi + alamat + Plus Code
- Estimasi azimuth sektor (arah pancar antena)
- Telegram Location native (titik di peta dalam chat)
- Tombol inline ke Google / OSM / Bing / Waze / Apple Maps

### Konfigurasi `.env`

| Variable | Default | Deskripsi |
|---|---|---|
| `TG_BOT_TOKEN` | - | Wajib. Dari @BotFather. |
| `TG_BOT_NAME` | LTE Cell Lookup | Nama di /start. |
| `TG_RATE_LIMIT_SEC` | 3 | Jeda min antar request per user (detik). |
| `TG_DEFAULT_MCC` | 510 | MCC default kalau user kasih 2 angka saja. |
| `TG_DEFAULT_MNC` | 10 | MNC default. |
| `TG_INCLUDE_LOCATION` | 1 | Kirim Telegram Location native. |
| `TG_INCLUDE_ADDRESS` | 1 | Tampilkan alamat. |
| `TG_INCLUDE_MAP_BUTTONS` | 1 | Tombol map links. |
| `TG_INCLUDE_PLUS_CODE` | 1 | Tampilkan Plus Code. |
| `TG_INCLUDE_AZIMUTH` | 1 | Tampilkan estimasi arah pancar sektor. |
| `SECTOR_AZIMUTHS` | 0,120,240 | Override azimuth global (CSV). |
| `SECTOR_AZIMUTHS_<MCC>_<MNC>` | - | Override per operator. |



## Database Lokal (OpenCellID / SQLite)

Aplikasi dilengkapi dukungan database lokal berbasis SQLite (`data/cells.db`) yang di-generate dari data OpenCellID Indonesia (`data/510.csv`).

### Keuntungan:
- **Respon Instan (< 1 ms)** tanpa latency jaringan.
- **Hemat Kuota API**: Sebagian besar tower Indonesia sudah tersedia offline, memangkas penggunaan kuota Unwired Labs hingga 90%+.
- **Tahan Gangguan**: Bot tetap bisa melayani pencarian tower lokal meskipun kuota API online habis.

### Hirarki Pencarian:
1. **Database Lokal** (`data/cells.db`): Mencari data offline dari OpenCellID.
2. **Cache File** (`cache/*.json`): Mengambil hasil query sebelumnya jika ada.
3. **OpenCellID API** (`opencellid.org/cell/get`): Lookup online, jatah
   **1.000 request/hari** per token. Butuh `OCID_TOKEN` dan **LAC/TAC** —
   kalau TAC tidak diketahui (misal hanya eNB + sektor), lapisan ini
   dilewati. Keunggulan: mencakup **seluruh dunia**, bukan cuma MCC 510
   seperti database lokal.
4. **Unwired Labs API**: Fallback terakhir, jatah 100 request/hari.
   Bisa lookup tanpa LAC.

Urutan ini sengaja menaruh OpenCellID sebelum Unwired Labs: kuotanya 10x
lebih besar, dan sumber datanya sama dengan database lokal sehingga
hasilnya konsisten. Set `OCID_LOOKUP=0` untuk menyimpan kuota OpenCellID
hanya bagi unduhan database harian.

### Impor Manual (Indonesia atau Seluruh Dunia):
Database otomatis dibuat saat bot atau CLI pertama kali dijalankan. Anda juga dapat mengimpor data manual atau memperbarui dengan dump CSV terbaru (misal `cell_towers_*.csv`):

```cmd
# 1. Impor data seluruh dunia (OpenCellID Worldwide Dump):
python import_csv.py data/cell_towers_2026-09-16-T000000.csv

# 2. Impor HANYA Indonesia (MCC 510) dari dump seluruh dunia:
python import_csv.py data/cell_towers_2026-09-16-T000000.csv --mcc 510

# 3. Impor data default (data/510.csv):
python import_csv.py
```

## Rumus CID

```
cid_full = eNB * 256 + sector
```

Itu yang dikirim ke Unwired Labs sebagai field `cid`.

## Deploy di VPS (Docker)

Jika aplikasi di-host di VPS menggunakan Docker:

```bash
git pull
docker compose up -d --build
```

Folder `data/` dan `cache/` sudah otomatis ter-mount sebagai Docker volume.

### Update Database Otomatis Harian

`update_db.py` mengunduh file negara terbaru dari OpenCellID, mengekstraknya,
mengimpor ulang ke SQLite, lalu menjalankan `VACUUM` + `ANALYZE`. Hanya
memakai pustaka standar, jadi tidak menambah dependency.

```bash
# Unduh + impor Indonesia (MCC 510), token dari OCID_TOKEN di .env
docker compose run --rm bot python update_db.py

# Negara lain
docker compose run --rm bot python update_db.py --mcc 502

# Tanpa unduh — impor ulang CSV yang sudah ada
docker compose run --rm bot python update_db.py --csv data/510.csv
```

Exit code 0 kalau sukses, 1 kalau gagal — aman dipakai untuk alerting.

#### Jadwalkan lewat cron

Export OpenCellID baru tersedia tiap hari **pukul 02:00 GMT** (09:00 WIB),
dan tiap file hanya boleh diunduh **2x per hari**. Jadwalkan sekitar
pukul 10:00 WIB supaya file sudah pasti tersedia.

**Pakai timezone server, bukan asumsi WIB.** Jadwal cron mengikuti jam
server. Cek dulu dengan `timedatectl`. Contoh konversi 10:00 WIB:

| Timezone server | Yang ditulis di crontab |
|---|---|
| WIB (UTC+7) | `0 10 * * *` |
| CST / Asia/Shanghai (UTC+8) | `0 11 * * *` |
| UTC | `0 3 * * *` |

Menulis `0 10` di server CST berarti job terpicu pukul 10:00 CST = 09:00
WIB — persis saat file terbit, tanpa margin. Kalau unduhan berjalan lebih
cepat dari jadwal server, `update_db.py` menerima respons "belum tersedia"
dan keluar dengan exit code 1.

Pasang di crontab user pemilik deploy (tanpa `sudo`), bukan root:

```cron
# Update database cell tower tiap hari, 11:00 CST = 10:00 WIB (03:00 GMT)
0 11 * * * cd /home/ubuntu/cell && /usr/bin/docker compose run --rm bot python update_db.py >> /home/ubuntu/cell/data/update.log 2>&1
```

Dua detail yang bikin cron gagal padahal di shell lancar:

- **PATH cron hanya `/usr/bin:/bin`.** Pakai path absolut (`/usr/bin/docker`).
  Kalau `which docker` menunjukkan `/usr/local/bin/docker`, ganti jadi itu.
- **Redirect pakai path absolut.** Pada `cd X && cmd >> log`, redirect baru
  aktif setelah `cd` sukses — kalau path salah, pesan errornya hilang ke
  mail lokal yang biasanya tidak ada MTA-nya, dan `update.log` tidak
  bertambah sama sekali.

Environment container sama dengan saat dijalankan manual, jadi `OCID_TOKEN`
dari `.env` tetap terbaca (via `env_file` di `docker-compose.yml`).

Bot **tidak perlu** dihentikan. Impor menulis ke file terpisah lalu
me-`rename`-nya, jadi bot yang sedang jalan tetap membaca DB lama sampai
selesai — tidak ada query yang putus di tengah.

Konsekuensinya, `VACUUM` bisa dilewati kalau bot sedang memegang koneksi
ke DB (VACUUM butuh akses eksklusif). Itu tidak masalah: data tetap
terimpor, hanya file-nya sedikit lebih besar.

Dalam praktiknya ini jarang terjadi: `db.py` membuka koneksi SQLite per
query lalu menutupnya, dan `ensure_db()` di startup juga menutup
koneksinya. Jadi bot tidak menyimpan koneksi lama yang menahan VACUUM.
Kalau ternyata VACUUM tetap dilewati (lihat log), hentikan bot dulu:

```cron
0 11 * * * cd /home/ubuntu/cell && docker compose stop bot && docker compose run --rm bot python update_db.py >> /home/ubuntu/cell/data/update.log 2>&1; docker compose start bot
```

Perhatikan `;` sebelum `docker compose start bot`: bot dinyalakan kembali
apa pun hasil updatenya, supaya bot tidak tinggal mati kalau update gagal.

**Catatan penting:**

- Impor bersifat **atomic** (tulis ke `cells.tmp`, baru di-`replace`), jadi
  kalau cron mati di tengah jalan `cells.db` lama tetap utuh. Konsekuensinya
  butuh ruang disk sekitar 2x ukuran DB akhir selama proses.
- `VACUUM` butuh ruang disk sebesar ukuran DB (SQLite menulis ulang file utuh).
  Pastikan `df -h` masih lega, terutama kalau pakai dump seluruh dunia.
- Secara default backup **tidak** dibuat tiap hari — menyalin DB multi-GB
  setiap hari membuang disk. Jalankan `--backup` sekali sebelum perubahan
  besar, atau andalkan sifat atomic di atas.
- Token OpenCellID sama dengan token Unwired Labs (format `pk.`). Kalau
  `OCID_TOKEN` kosong, script memakai token pertama di `UWL_TOKEN`.


