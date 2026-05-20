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
- Alamat terstruktur: jalan, kecamatan, kota, kode pos
- Link ke Google Maps, OpenStreetMap, Bing, Waze, Apple Maps
- Fallback flag (`cidf` = exact CID, `lacf` = approximate via LAC)

## Cache

Hasil sukses disimpan di `cache/*.json` selama 30 hari. Lookup berulang
tidak menghabiskan kuota. Cache dimatikan untuk satu run dengan `--no-cache`,
atau hapus folder `cache/` untuk reset total.

## Rumus CID

```
cid_full = eNB * 256 + sector
```

Itu yang dikirim ke Unwired Labs sebagai field `cid`.
