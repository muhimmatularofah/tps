# Dashboard Validasi Geocoding TPS Surabaya

Peta Streamlit untuk mengecek apakah titik lat/lon hasil geocoder jatuh di RW yang
diperkirakan dari `alamat_asli`, memakai batas RT/RW dari SLS BPS Kota Surabaya.

## Menjalankan

```bash
pip install -r requirements.txt
streamlit run app.py
```

Batas RT/RW sudah disiapkan di folder `data/` (hasil `prep_sls.py` dari
`SLS_KOTA_SURABAYA.shp`). Kalau shapefile SLS diperbarui, jalankan ulang:

```bash
python prep_sls.py path/ke/SLS_KOTA_SURABAYA.shp
```

## Data TPS

Di sidebar, tempel link Google Sheets (harus dibagikan sebagai "Siapa saja yang
memiliki link") atau upload file .xlsx/.csv. Kolom yang dipakai:

| Kolom | Fungsi |
|---|---|
| `lat`, `lon` | titik geocoder (format `-7,25` atau `-7.25` sama-sama diterima) |
| `rw_final` | RW harapan (cadangan: `rw_pasti`, `rw`, `rw_kandidat`) |
| `nmkec_sls`, `nmdesa_sls` | kelurahan harapan (cadangan: `kecamatan`, `kelurahan`, `kode_kelurahan`) |
| `rt` | opsional, untuk membandingkan RT |
| `alamat_asli`, `tps`, `grade`, `status`, dll. | ditampilkan di popup dan tabel |

## Cara penilaian

Setiap titik di-overlay ke poligon RT SLS, lalu jaraknya dihitung (dalam meter,
UTM 49S) ke poligon RW harapan:

- **Sesuai RW**: titik di dalam RW harapan
- **Dekat batas RW**: di luar RW harapan, tapi masih dalam toleransi (atur di sidebar, default 50 m)
- **Beda RW (kelurahan sama)**: di kelurahan yang benar, RW lain
- **Di luar kelurahan**: titik jatuh di kelurahan lain atau di luar Surabaya
- **RW harapan tidak ada / Kelurahan tak dikenal**: data harapannya kosong atau tidak cocok dengan SLS

Kolom `titik_dipakai_bersama` menunjukkan berapa TPS berbagi koordinat yang persis
sama; angka > 1 biasanya tanda geocoder jatuh ke centroid.

Tab **Cek massal ke OSM** dan tombol di panel kanan melakukan geocode ulang
`alamat_asli` lewat OpenStreetMap (Nominatim, 1 permintaan/detik) sebagai
pembanding jarak kedua.
