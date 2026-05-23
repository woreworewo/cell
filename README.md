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

- `/cell 510 10 11071 1` - format lengkap
- `/cell 11071 1` - pakai default MCC/MNC dari `.env`
- `/cell 510-10-11071-1` - separator alternatif (`-`, `/`, `,`, `_`)
- `/enb 11071` - sweep semua sektor sekaligus, list azimuth tiap sektor
- `/start` atau `/help` - bantuan

**Bonus arah sinyal:** setelah `/cell` atau `/enb` sukses, share lokasi
kamu lewat klip 📎 → Location. Bot akan balas dengan jarak ke tower,
bearing arah tower, dan tebakan sektor mana yang seharusnya melayani
posisi kamu.

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
| `TG_RATE_LIMIT_SEC` | 300 | Jeda min antar request per user (detik). |
| `TG_DEFAULT_MCC` | 510 | MCC default kalau user kasih 2 angka saja. |
| `TG_DEFAULT_MNC` | 10 | MNC default. |
| `TG_INCLUDE_LOCATION` | 1 | Kirim Telegram Location native. |
| `TG_INCLUDE_ADDRESS` | 1 | Tampilkan alamat. |
| `TG_INCLUDE_MAP_BUTTONS` | 1 | Tombol map links. |
| `TG_INCLUDE_PLUS_CODE` | 1 | Tampilkan Plus Code. |
| `TG_INCLUDE_AZIMUTH` | 1 | Tampilkan estimasi arah pancar sektor. |
| `SECTOR_AZIMUTHS` | 0,120,240 | Override azimuth global (CSV). |
| `SECTOR_AZIMUTHS_<MCC>_<MNC>` | - | Override per operator. |



## Rumus CID

```
cid_full = eNB * 256 + sector
```

Itu yang dikirim ke Unwired Labs sebagai field `cid`.
