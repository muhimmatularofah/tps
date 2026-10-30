"""
Dashboard validasi geocoding TPS Kota Surabaya terhadap batas RT/RW (SLS BPS).

Menjawab pertanyaan: apakah titik lat/lon hasil geocoder jatuh di RW yang
diperkirakan dari alamat_asli, atau malah jauh dari sana?

Jalankan:
    pip install -r requirements.txt
    streamlit run app.py
"""
import io
import re
import time
from pathlib import Path

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import streamlit as st
from shapely.geometry import Point
from shapely.ops import nearest_points
from streamlit_folium import st_folium

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
DEFAULT_SHEET = ("https://docs.google.com/spreadsheets/d/"
                 "1izvfW9KfqoI304uFYaYCZxaVFTQzdldN/edit?usp=sharing")
METRIC_CRS = 32749  # UTM 49S, satuan meter, cocok untuk Surabaya

# Warna status (good / warning / serious / critical) + netral.
# Selalu ditampilkan bersama label teks, tidak pernah warna saja.
KATEGORI = {
    "Sesuai RW":               {"warna": "#0ca30c", "ikon": "✅", "urut": 0},
    "Dekat batas RW":          {"warna": "#fab219", "ikon": "⚠️", "urut": 1},
    "Beda RW (kelurahan sama)": {"warna": "#ec835a", "ikon": "🟠", "urut": 2},
    "Di luar kelurahan":       {"warna": "#d03b3b", "ikon": "❌", "urut": 3},
    "RW harapan tidak ada":    {"warna": "#8a8a85", "ikon": "❔", "urut": 4},
    "Kelurahan tak dikenal":   {"warna": "#5a5a56", "ikon": "❔", "urut": 5},
    "Tanpa koordinat":         {"warna": "#bdbdb5", "ikon": "∅", "urut": 6},
}

st.set_page_config(page_title="Validasi Geocoding TPS Surabaya", page_icon="🗺️",
                   layout="wide")


# --------------------------------------------------------------------------- #
# Utilitas
# --------------------------------------------------------------------------- #
def norm_name(s) -> str:
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def first_int(v):
    """'01' -> 1, 'RW 04' -> 4, '3.0' -> 3, '2;5' -> 2, '' -> None."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def to_float(v):
    """Menerima angka biasa atau format Indonesia '-7,2529788'."""
    if v is None:
        return np.nan
    if isinstance(v, (int, float, np.number)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        return np.nan
    if "," in s and "." in s:          # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return np.nan


def sheet_export_url(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        return url
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns]
    return df


def pick(df, *names):
    for n in names:
        if n in df.columns:
            return n
    return None


# --------------------------------------------------------------------------- #
# Muat data
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner="Memuat batas RT/RW (SLS)…")
def load_sls():
    rt_p, rw_p = DATA_DIR / "sls_rt.parquet", DATA_DIR / "sls_rw.parquet"
    if not (rt_p.exists() and rw_p.exists()):
        shp = next(APP_DIR.rglob("SLS_KOTA_SURABAYA.shp"), None)
        if shp is None:
            st.error("Data SLS belum ada. Taruh SLS_KOTA_SURABAYA.shp di folder app "
                     "lalu jalankan `python prep_sls.py SLS_KOTA_SURABAYA.shp`.")
            st.stop()
        import prep_sls
        prep_sls.main(str(shp), str(DATA_DIR))
    rt = gpd.read_parquet(rt_p)
    rw = gpd.read_parquet(rw_p)
    rw["rw"] = rw["rw"].astype(int)
    return rt, rw


@st.cache_data(ttl=600, show_spinner="Mengunduh spreadsheet…")
def load_sheet_from_url(url: str) -> pd.DataFrame:
    if Path(url).exists():  # path file lokal juga diterima
        p = Path(url)
        return load_sheet_from_bytes(p.name, p.read_bytes())
    r = requests.get(sheet_export_url(url), timeout=60)
    r.raise_for_status()
    if r.content[:2] != b"PK":
        raise ValueError("Respons bukan file Excel. Pastikan link dibagikan "
                         "'Siapa saja yang memiliki link'.")
    return pd.read_excel(io.BytesIO(r.content), dtype=str)


@st.cache_data(show_spinner="Membaca file…")
def load_sheet_from_bytes(name: str, data: bytes) -> pd.DataFrame:
    if name.lower().endswith(".csv"):
        return pd.read_csv(io.BytesIO(data), dtype=str)
    return pd.read_excel(io.BytesIO(data), dtype=str)


# --------------------------------------------------------------------------- #
# Analisis spasial
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner="Menghitung posisi titik terhadap RT/RW…")
def analyze(raw: pd.DataFrame, toleransi_m: float) -> pd.DataFrame:
    rt, rw = load_sls()
    df = clean_columns(raw)
    df = df[df.notna().any(axis=1)].reset_index(drop=True)

    c_lat, c_lon = pick(df, "lat", "latitude"), pick(df, "lon", "lng", "longitude")
    if not (c_lat and c_lon):
        raise KeyError("Kolom lat/lon tidak ditemukan di spreadsheet.")
    c_kec = pick(df, "nmkec_sls", "kecamatan")
    c_desa = pick(df, "nmdesa_sls", "kelurahan")
    c_kode = pick(df, "kode_kelurahan")
    c_rw = pick(df, "rw_final", "rw_pasti", "rw", "rw_kandidat")
    c_rt = pick(df, "rt", "rt_final")

    out = pd.DataFrame(index=df.index)
    out["lat_f"] = df[c_lat].map(to_float)
    out["lon_f"] = df[c_lon].map(to_float)
    # Koordinat kebalik? (lat ~112, lon ~-7) -> tukar
    swap = out["lat_f"].abs().gt(90) & out["lon_f"].abs().lt(90)
    out.loc[swap, ["lat_f", "lon_f"]] = out.loc[swap, ["lon_f", "lat_f"]].values

    # --- kelurahan harapan (dari sheet) -> kode_desa SLS
    desa_lookup = rw[["kode_desa", "kec_key", "desa_key", "nmkec", "nmdesa"]].drop_duplicates("kode_desa")
    by_kode = set(desa_lookup["kode_desa"])
    by_pair = {(r.kec_key, r.desa_key): r.kode_desa for r in desa_lookup.itertuples()}
    by_desa = desa_lookup.groupby("desa_key")["kode_desa"].agg(list).to_dict()

    def resolve(i):
        if c_kode:
            k = re.sub(r"\D", "", str(df.at[i, c_kode] or ""))
            if k in by_kode:
                return k
        kec = norm_name(df.at[i, c_kec]) if c_kec else ""
        desa = norm_name(df.at[i, c_desa]) if c_desa else ""
        # fallback ke kolom asli bila kolom *_sls kosong
        if not desa and "kelurahan" in df.columns:
            desa = norm_name(df.at[i, "kelurahan"])
        if not kec and "kecamatan" in df.columns:
            kec = norm_name(df.at[i, "kecamatan"])
        if (kec, desa) in by_pair:
            return by_pair[(kec, desa)]
        cands = by_desa.get(desa, [])
        return cands[0] if len(cands) == 1 else None

    out["kode_desa_harap"] = [resolve(i) for i in df.index]
    out["rw_harap"] = df[c_rw].map(first_int) if c_rw else None
    out["rt_harap"] = df[c_rt].map(first_int) if c_rt else None

    # --- titik -> RT/RW aktual (spatial join)
    has_xy = out["lat_f"].notna() & out["lon_f"].notna()
    pts = gpd.GeoDataFrame(out[has_xy].copy(),
                           geometry=gpd.points_from_xy(out.loc[has_xy, "lon_f"],
                                                       out.loc[has_xy, "lat_f"]),
                           crs=4326)
    j = gpd.sjoin(pts, rt[["kode_desa", "nmkec", "nmdesa", "nmsls", "rt", "rw", "geometry"]],
                  how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    for src, dst in [("kode_desa", "kode_desa_aktual"), ("nmkec", "kec_aktual"),
                     ("nmdesa", "kel_aktual"), ("nmsls", "sls_aktual"),
                     ("rt", "rt_aktual"), ("rw", "rw_aktual")]:
        out[dst] = j[src].reindex(out.index)

    # --- jarak titik ke poligon RW harapan
    rw_idx = rw.set_index(["kode_desa", "rw"])
    rw_m = rw.to_crs(METRIC_CRS).set_index(["kode_desa", "rw"])
    pts_m = pts.to_crs(METRIC_CRS)

    jarak, jarak_pusat, near_lat, near_lon = {}, {}, {}, {}
    for i, p in pts_m.geometry.items():
        key = (out.at[i, "kode_desa_harap"], out.at[i, "rw_harap"])
        if key[0] is None or pd.isna(key[1]) or key not in rw_m.index:
            continue
        poly = rw_m.loc[key, "geometry"]
        d = p.distance(poly)
        jarak[i] = d
        jarak_pusat[i] = p.distance(poly.representative_point())
        if d > 0:
            q = nearest_points(p, poly)[1]
            q4326 = gpd.GeoSeries([q], crs=METRIC_CRS).to_crs(4326).iloc[0]
            near_lat[i], near_lon[i] = q4326.y, q4326.x
    out["jarak_ke_rw_m"] = pd.Series(jarak, dtype=float)
    out["jarak_ke_pusat_rw_m"] = pd.Series(jarak_pusat, dtype=float)
    out["titik_terdekat_lat"] = pd.Series(near_lat, dtype=float)
    out["titik_terdekat_lon"] = pd.Series(near_lon, dtype=float)
    out["rw_harap_ada"] = [
        (k, r) in rw_idx.index if k is not None and not pd.isna(r) else False
        for k, r in zip(out["kode_desa_harap"], out["rw_harap"])]

    # --- titik dipakai bersama (indikasi fallback centroid)
    xy_key = out["lat_f"].round(6).astype(str) + "," + out["lon_f"].round(6).astype(str)
    out["titik_dipakai_bersama"] = xy_key.map(xy_key[has_xy].value_counts()).fillna(0).astype(int)

    # --- kategori
    def kategori(r):
        if pd.isna(r.lat_f) or pd.isna(r.lon_f):
            return "Tanpa koordinat"
        if r.kode_desa_harap is None:
            return "Kelurahan tak dikenal"
        if r.kode_desa_aktual != r.kode_desa_harap:
            return "Di luar kelurahan"
        if not r.rw_harap_ada:
            return "RW harapan tidak ada"
        if r.jarak_ke_rw_m == 0:
            return "Sesuai RW"
        if r.jarak_ke_rw_m <= toleransi_m:
            return "Dekat batas RW"
        return "Beda RW (kelurahan sama)"

    out["kategori"] = out.apply(kategori, axis=1)
    out["rt_cocok"] = np.where(
        out["rt_harap"].notna() & out["rt_aktual"].notna(),
        out["rt_harap"] == out["rt_aktual"], None)

    res = pd.concat([df, out], axis=1)
    res["_label"] = [
        f"{i + 1}. {str(r.get('kelurahan', '') or '')} · TPS {r.get('tps', '')} · "
        f"{str(r.get('alamat_asli', '') or '')[:60]}"
        for i, (_, r) in enumerate(res.iterrows())]
    return res


# --------------------------------------------------------------------------- #
# Geocode ulang alamat_asli via OpenStreetMap (opsional, pembanding)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False, ttl=86400)
def nominatim(q: str):
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q, "format": "json", "limit": 1, "countrycodes": "id",
                "viewbox": "112.55,-7.15,112.88,-7.37", "bounded": 1},
        headers={"User-Agent": "validasi-tps-surabaya/1.0 (streamlit dashboard)"},
        timeout=20)
    r.raise_for_status()
    js = r.json()
    if not js:
        return None
    return float(js[0]["lat"]), float(js[0]["lon"]), js[0].get("display_name", "")


def jarak_m(lat1, lon1, lat2, lon2):
    g = gpd.GeoSeries([Point(lon1, lat1), Point(lon2, lat2)], crs=4326).to_crs(METRIC_CRS)
    return g.iloc[0].distance(g.iloc[1])


def osm_query(r) -> str:
    parts = [r.get("alamat_asli"), r.get("kelurahan"), r.get("kecamatan"), "Surabaya"]
    return ", ".join(str(p) for p in parts if p and str(p).strip() and str(p) != "nan")


# --------------------------------------------------------------------------- #
# Peta
# --------------------------------------------------------------------------- #
def popup_html(r) -> str:
    def v(k):
        x = r.get(k)
        return "" if x is None or (isinstance(x, float) and np.isnan(x)) else x
    jarak = r["jarak_ke_rw_m"]
    jarak_txt = "-" if pd.isna(jarak) else f"{jarak:,.0f} m"
    info = KATEGORI[r["kategori"]]
    rw_aktual = "-" if pd.isna(r["rw_aktual"]) else f"RW {int(r['rw_aktual']):02d}"
    rw_harap = "-" if pd.isna(r["rw_harap"]) else f"RW {int(r['rw_harap']):02d}"
    return f"""
    <div style="font-family:sans-serif;font-size:12px;min-width:260px">
      <b>TPS {v('tps')} · {v('kelurahan')}</b><br>
      <span style="color:#555">{v('alamat_asli')}</span><hr style="margin:4px 0">
      {info['ikon']} <b>{r['kategori']}</b><br>
      RW harapan: <b>{rw_harap}</b> · titik jatuh di: <b>{rw_aktual}</b>
      ({v('kel_aktual') or 'luar SLS'})<br>
      SLS titik: {v('sls_aktual') or '-'}<br>
      Jarak ke batas RW harapan: <b>{jarak_txt}</b><br>
      Grade: {v('grade')} · {v('status')}<br>
      Titik dipakai {int(r['titik_dipakai_bersama'])} TPS
    </div>"""


def build_map(view: pd.DataFrame, rt, rw, kode_fokus, selected=None, osm=None,
              tampil_rt=True):
    if selected is not None and not pd.isna(selected["lat_f"]):
        center, zoom = [selected["lat_f"], selected["lon_f"]], 17
    elif view["lat_f"].notna().any():
        center = [view["lat_f"].median(), view["lon_f"].median()]
        zoom = 15 if len(kode_fokus) <= 1 else 13
    else:
        center, zoom = [-7.2575, 112.7521], 12

    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(
        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr="&copy; OpenStreetMap &copy; CARTO", name="Peta terang").add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Citra satelit").add_to(m)

    rw_sel = rw[rw["kode_desa"].isin(kode_fokus)]
    rt_sel = rt[rt["kode_desa"].isin(kode_fokus)]

    if tampil_rt and len(rt_sel):
        folium.GeoJson(
            rt_sel[["nmdesa", "nmsls", "geometry"]], name="Batas RT",
            style_function=lambda f: {"color": "#9a9a92", "weight": 0.7,
                                      "fillOpacity": 0, "dashArray": "3 3"},
            tooltip=folium.GeoJsonTooltip(["nmdesa", "nmsls"], aliases=["Kelurahan", "SLS"]),
        ).add_to(m)

    if len(rw_sel):
        rw_show = rw_sel.assign(label=lambda d: "RW " + d["rw"].astype(str).str.zfill(2))
        folium.GeoJson(
            rw_show[["nmdesa", "label", "jumlah_rt", "geometry"]], name="Batas RW",
            style_function=lambda f: {"color": "#2b2b28", "weight": 2,
                                      "fillColor": "#2b2b28", "fillOpacity": 0.03},
            highlight_function=lambda f: {"weight": 3, "fillOpacity": 0.12},
            tooltip=folium.GeoJsonTooltip(["nmdesa", "label", "jumlah_rt"],
                                          aliases=["Kelurahan", "RW", "Jumlah RT"]),
        ).add_to(m)
        # label RW di tengah poligon
        lbl = folium.FeatureGroup(name="Label RW")
        for r in rw_show.itertuples():
            p = r.geometry.representative_point()
            folium.Marker(
                [p.y, p.x],
                icon=folium.DivIcon(html=f'<div style="font:600 11px sans-serif;color:#2b2b28;'
                                         f'text-shadow:0 0 3px #fff,0 0 3px #fff;'
                                         f'white-space:nowrap;transform:translate(-50%,-50%)">'
                                         f'{r.label}</div>')).add_to(lbl)
        lbl.add_to(m)

    # Sorot RW harapan untuk TPS terpilih + garis ke batas terdekat
    if selected is not None and selected["rw_harap_ada"]:
        target = rw[(rw["kode_desa"] == selected["kode_desa_harap"])
                    & (rw["rw"] == int(selected["rw_harap"]))]
        folium.GeoJson(
            target[["geometry"]], name="RW harapan (terpilih)",
            style_function=lambda f: {"color": "#1f6feb", "weight": 3,
                                      "fillColor": "#1f6feb", "fillOpacity": 0.15},
        ).add_to(m)
        if not pd.isna(selected["lat_f"]):
            minx, miny, maxx, maxy = target.total_bounds
            m.fit_bounds([[min(miny, selected["lat_f"]), min(minx, selected["lon_f"])],
                          [max(maxy, selected["lat_f"]), max(maxx, selected["lon_f"])]],
                         padding=(30, 30))
        if not pd.isna(selected["titik_terdekat_lat"]):
            folium.PolyLine(
                [[selected["lat_f"], selected["lon_f"]],
                 [selected["titik_terdekat_lat"], selected["titik_terdekat_lon"]]],
                color="#1f6feb", weight=2, dash_array="6 4",
                tooltip=f"{selected['jarak_ke_rw_m']:,.0f} m ke batas RW harapan").add_to(m)

    # Titik TPS
    fg = folium.FeatureGroup(name="Titik TPS (geocoder)")
    for _, r in view[view["lat_f"].notna()].iterrows():
        info = KATEGORI[r["kategori"]]
        is_sel = selected is not None and r.name == selected.name
        folium.CircleMarker(
            [r["lat_f"], r["lon_f"]],
            radius=10 if is_sel else 6,
            color="#ffffff" if not is_sel else "#1f6feb", weight=3 if is_sel else 2,
            fill=True, fill_color=info["warna"], fill_opacity=0.95,
            tooltip=f"TPS {r.get('tps', '')} · {r['kategori']}",
            popup=folium.Popup(popup_html(r), max_width=340),
        ).add_to(fg)
    fg.add_to(m)

    # Pembanding OSM untuk alamat_asli
    if osm is not None and selected is not None:
        lat, lon, nama = osm
        folium.Marker([lat, lon], icon=folium.Icon(color="purple", icon="home"),
                      tooltip="alamat_asli menurut OpenStreetMap",
                      popup=folium.Popup(nama, max_width=300)).add_to(m)
        if not pd.isna(selected["lat_f"]):
            folium.PolyLine([[lat, lon], [selected["lat_f"], selected["lon_f"]]],
                            color="#7b2cbf", weight=2, dash_array="2 6").add_to(m)

    folium.LayerControl(collapsed=True).add_to(m)
    return m


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
rt, rw = load_sls()

with st.sidebar:
    st.header("Sumber data")
    sumber = st.radio("Ambil data TPS dari", ["Link Google Sheets", "Upload file"],
                      horizontal=True)
    raw = None
    try:
        if sumber == "Link Google Sheets":
            url = st.text_input("Link spreadsheet", DEFAULT_SHEET)
            if st.button("Muat ulang dari Google Sheets"):
                load_sheet_from_url.clear()
            raw = load_sheet_from_url(url)
        else:
            up = st.file_uploader("File .xlsx / .csv", type=["xlsx", "xls", "csv"])
            if up:
                raw = load_sheet_from_bytes(up.name, up.getvalue())
    except Exception as e:  # noqa: BLE001
        st.error(f"Gagal memuat data: {e}")

    st.header("Aturan penilaian")
    toleransi = st.slider("Toleransi 'dekat batas RW' (meter)", 0, 300, 50, 10,
                          help="Titik di luar RW harapan tetapi masih sedekat ini "
                               "dari batasnya dianggap 'Dekat batas RW'.")

st.title("Validasi Geocoding TPS terhadap Batas RT/RW")
st.caption("Apakah titik lat/lon dari geocoder jatuh di RW yang diperkirakan dari "
           "`alamat_asli`? Batas wilayah: SLS BPS Kota Surabaya.")

if raw is None:
    st.info("Muat data TPS lewat sidebar untuk mulai. Link Google Sheets harus "
            "dibagikan sebagai **Siapa saja yang memiliki link → Viewer**.")
    st.stop()

try:
    res = analyze(raw, float(toleransi))
except Exception as e:  # noqa: BLE001
    st.error(f"Gagal menganalisis data: {e}")
    st.stop()

# ---------------- Filter ----------------
with st.sidebar:
    st.header("Filter")
    kec_col = "kecamatan" if "kecamatan" in res.columns else None
    kel_col = "kelurahan" if "kelurahan" in res.columns else None
    kec_opts = sorted(res[kec_col].dropna().unique()) if kec_col else []
    kec_pick = st.selectbox("Kecamatan", ["(Semua)"] + kec_opts)
    sub = res if kec_pick == "(Semua)" else res[res[kec_col] == kec_pick]
    kel_opts = sorted(sub[kel_col].dropna().unique()) if kel_col else []
    kel_pick = st.selectbox("Kelurahan", ["(Semua)"] + kel_opts)
    if kel_pick != "(Semua)":
        sub = sub[sub[kel_col] == kel_pick]

    kat_order = sorted(KATEGORI, key=lambda k: KATEGORI[k]["urut"])
    kat_pick = st.multiselect("Kategori", kat_order, default=kat_order)
    sub = sub[sub["kategori"].isin(kat_pick)]

    if "grade" in sub.columns:
        g_opts = sorted(sub["grade"].dropna().astype(str).unique())
        g_pick = st.multiselect("Grade geocoder", g_opts, default=g_opts)
        sub = sub[sub["grade"].astype(str).isin(g_pick) | sub["grade"].isna()]

    min_jarak = st.number_input("Hanya jarak ke RW harapan ≥ (m)", 0, 50000, 0, 50)
    if min_jarak > 0:
        sub = sub[sub["jarak_ke_rw_m"].fillna(-1) >= min_jarak]

    tampil_rt = st.checkbox("Tampilkan batas RT", value=True)

# ---------------- Ringkasan ----------------
scope = res if kec_pick == "(Semua)" else res[res[kec_col] == kec_pick]
if kel_pick != "(Semua)":
    scope = scope[scope[kel_col] == kel_pick]

counts = scope["kategori"].value_counts()
total = len(scope)
cols = st.columns(5)
ringkas = [
    ("Total TPS", total, None),
    ("✅ Sesuai RW", counts.get("Sesuai RW", 0), "#0ca30c"),
    ("⚠️ Dekat batas", counts.get("Dekat batas RW", 0), "#fab219"),
    ("🟠 Beda RW", counts.get("Beda RW (kelurahan sama)", 0), "#ec835a"),
    ("❌ Luar kelurahan", counts.get("Di luar kelurahan", 0), "#d03b3b"),
]
for c, (lbl, val, _) in zip(cols, ringkas):
    c.metric(lbl, f"{val:,}")
    if lbl != "Total TPS" and total:
        c.caption(f"{val / total:.0%} dari total")

lain = total - sum(v for _, v, _ in ringkas[1:])
if lain:
    st.caption(f"{lain} TPS lain tidak bisa dinilai: "
               + ", ".join(f"{k} ({counts.get(k, 0)})" for k in
                           ["RW harapan tidak ada", "Kelurahan tak dikenal", "Tanpa koordinat"]
                           if counts.get(k, 0)))

# ---------------- Peta + detail ----------------
if kel_pick != "(Semua)":
    kode_fokus = set(sub["kode_desa_harap"].dropna()) | set(scope["kode_desa_harap"].dropna())
elif kec_pick != "(Semua)":
    kode_fokus = set(rw.loc[rw["kec_key"] == norm_name(kec_pick), "kode_desa"])
    kode_fokus |= set(scope["kode_desa_harap"].dropna())
else:
    kode_fokus = set()  # seluruh kota: titik saja agar ringan
# Kelurahan tempat titik nyasar juga ditampilkan batasnya
kode_fokus |= set(sub.loc[sub["kategori"] == "Di luar kelurahan", "kode_desa_aktual"].dropna()) \
    if kel_pick != "(Semua)" else set()

left, right = st.columns([0.66, 0.34])

with right:
    st.subheader("Periksa satu TPS")
    urut = sub.assign(_k=sub["kategori"].map(lambda k: KATEGORI[k]["urut"]))
    urut = urut.sort_values(["_k", "jarak_ke_rw_m"], ascending=[False, False])
    pilihan = ["(Tidak ada)"] + urut["_label"].tolist()
    pick_lbl = st.selectbox("TPS (paling bermasalah di atas)", pilihan)
    selected = None if pick_lbl == "(Tidak ada)" else res[res["_label"] == pick_lbl].iloc[0]

    osm = None
    if selected is not None:
        info = KATEGORI[selected["kategori"]]
        st.markdown(f"**{info['ikon']} {selected['kategori']}**")
        st.markdown(f"**Alamat asli:** {selected.get('alamat_asli', '')}")
        if selected.get("bangunan"):
            st.markdown(f"**Bangunan:** {selected.get('bangunan')}")
        rwh = "-" if pd.isna(selected["rw_harap"]) else f"RW {int(selected['rw_harap']):02d}"
        rwa = "-" if pd.isna(selected["rw_aktual"]) else f"RW {int(selected['rw_aktual']):02d}"
        d = selected["jarak_ke_rw_m"]
        dp = selected["jarak_ke_pusat_rw_m"]
        st.markdown(
            f"| Cek | Hasil |\n|---|---|\n"
            f"| RW harapan | **{rwh}** ({selected.get('kelurahan', '')}) |\n"
            f"| Titik jatuh di | **{selected['sls_aktual'] if pd.notna(selected['sls_aktual']) else 'luar SLS'}** "
            f"· {selected['kel_aktual'] if pd.notna(selected['kel_aktual']) else '-'} |\n"
            f"| Jarak ke batas RW harapan | **{'-' if pd.isna(d) else f'{d:,.0f} m'}** |\n"
            f"| Jarak ke tengah RW harapan | {'-' if pd.isna(dp) else f'{dp:,.0f} m'} |\n"
            f"| Grade / status geocoder | {selected.get('grade', '-')} · {selected.get('status', '-')} |\n"
            f"| Titik dipakai bersama | {int(selected['titik_dipakai_bersama'])} TPS |")
        if selected["titik_dipakai_bersama"] > 1:
            st.caption("Titik yang sama dipakai beberapa TPS biasanya berarti geocoder "
                       "jatuh ke centroid/titik cadangan, bukan alamat sebenarnya.")
        for k in ["catatan_rw", "rw_kandidat", "hasil_geocoder", "metode", "perlu_review"]:
            if k in selected.index and pd.notna(selected[k]) and str(selected[k]).strip():
                st.markdown(f"**{k}:** {selected[k]}")

        st.divider()
        st.markdown("**Bandingkan dengan geocode ulang `alamat_asli` (OpenStreetMap)**")
        q = st.text_input("Kueri", osm_query(selected), key=f"q_{selected.name}")
        if st.button("Cari di OpenStreetMap"):
            try:
                osm = nominatim(q)
                st.session_state[f"osm_{selected.name}"] = osm
            except Exception as e:  # noqa: BLE001
                st.error(f"Nominatim gagal: {e}")
        osm = st.session_state.get(f"osm_{selected.name}", osm)
        if osm:
            dd = jarak_m(osm[0], osm[1], selected["lat_f"], selected["lon_f"]) \
                if not pd.isna(selected["lat_f"]) else None
            st.success(f"OSM: {osm[2][:120]}…" if len(osm[2]) > 120 else f"OSM: {osm[2]}")
            if dd is not None:
                st.metric("Jarak titik geocoder ↔ alamat_asli (OSM)", f"{dd:,.0f} m")
        elif osm is None and f"osm_{selected.name}" in st.session_state:
            st.warning("Alamat tidak ditemukan di OpenStreetMap.")

    st.divider()
    st.markdown("**Keterangan warna titik**")
    st.markdown("".join(
        f'<div style="display:flex;align-items:center;gap:8px;margin:2px 0;font-size:13px">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{v["warna"]};'
        f'box-shadow:0 0 0 2px #fff, 0 0 0 3px rgba(0,0,0,.15)"></span>{k}</div>'
        for k, v in KATEGORI.items() if k != "Tanpa koordinat"), unsafe_allow_html=True)
    st.caption("Garis biru putus-putus: dari titik ke batas terdekat RW harapan. "
               "Area biru: RW harapan TPS terpilih.")

with left:
    if not kode_fokus:
        st.caption("Pilih kecamatan atau kelurahan di sidebar untuk menampilkan batas RT/RW.")
    if selected is not None:
        kode_fokus = kode_fokus | {selected["kode_desa_harap"], selected["kode_desa_aktual"]}
        kode_fokus = {k for k in kode_fokus if isinstance(k, str)}
    m = build_map(sub, rt, rw, kode_fokus, selected, osm, tampil_rt)
    st_folium(m, height=640, use_container_width=True, returned_objects=[])

# ---------------- Tabel & rekap ----------------
tab1, tab2, tab3 = st.tabs(["Tabel TPS", "Rekap per kelurahan", "Cek massal ke OSM"])

show_cols = [c for c in [
    "kecamatan", "kelurahan", "tps", "alamat_asli", "bangunan", "kategori",
    "rw_harap", "rw_aktual", "rt_harap", "rt_aktual", "sls_aktual", "kel_aktual",
    "jarak_ke_rw_m", "jarak_ke_pusat_rw_m", "titik_dipakai_bersama",
    "grade", "status", "metode", "lat_f", "lon_f"] if c in sub.columns]

with tab1:
    st.dataframe(
        sub[show_cols].sort_values("jarak_ke_rw_m", ascending=False, na_position="last"),
        width="stretch", hide_index=True,
        column_config={
            "jarak_ke_rw_m": st.column_config.NumberColumn("Jarak ke RW harapan (m)", format="%.0f"),
            "jarak_ke_pusat_rw_m": st.column_config.NumberColumn("Jarak ke tengah RW (m)", format="%.0f"),
            "rw_harap": st.column_config.NumberColumn("RW harapan", format="%d"),
            "rw_aktual": st.column_config.NumberColumn("RW titik", format="%d"),
            "rt_harap": st.column_config.NumberColumn("RT harapan", format="%d"),
            "rt_aktual": st.column_config.NumberColumn("RT titik", format="%d"),
            "lat_f": st.column_config.NumberColumn("lat", format="%.6f"),
            "lon_f": st.column_config.NumberColumn("lon", format="%.6f"),
        })
    export = res.drop(columns=["_label", "titik_terdekat_lat", "titik_terdekat_lon"])
    st.download_button("Unduh hasil validasi (CSV, semua TPS)",
                       export.to_csv(index=False).encode("utf-8-sig"),
                       "validasi_geocoding_tps.csv", "text/csv")

with tab2:
    grp_cols = [c for c in ["kecamatan", "kelurahan"] if c in res.columns]
    if grp_cols:
        rek = (scope.groupby(grp_cols + ["kategori"]).size().unstack(fill_value=0))
        rek = rek[[k for k in kat_order if k in rek.columns]]
        rek["Total"] = rek.sum(axis=1)
        if "Sesuai RW" in rek.columns:
            rek["% sesuai RW"] = (rek["Sesuai RW"] / rek["Total"] * 100).round(1)
            rek = rek.sort_values("% sesuai RW")
        st.dataframe(rek, width="stretch",
                     column_config={"% sesuai RW": st.column_config.ProgressColumn(
                         "% sesuai RW", min_value=0, max_value=100, format="%.1f%%")})

with tab3:
    st.markdown(
        "Geocode ulang `alamat_asli` lewat OpenStreetMap (Nominatim) untuk TPS yang "
        "sedang terfilter, lalu hitung jaraknya ke titik geocoder dan cek RW-nya. "
        "Nominatim membatasi 1 permintaan per detik, jadi 100 TPS ≈ 2 menit.")
    batas = st.number_input("Maksimum TPS", 1, 500, min(50, max(1, len(sub))))
    if st.button(f"Jalankan untuk {min(batas, len(sub))} TPS terfilter"):
        rows = sub.head(int(batas))
        bar = st.progress(0.0)
        hasil = []
        for n, (idx, r) in enumerate(rows.iterrows(), 1):
            q = osm_query(r)
            try:
                o = nominatim(q)
            except Exception:  # noqa: BLE001
                o = None
            rec = {"idx": idx, "tps": r.get("tps"), "kelurahan": r.get("kelurahan"),
                   "alamat_asli": r.get("alamat_asli"), "kategori": r["kategori"],
                   "osm_lat": None, "osm_lon": None, "osm_nama": None,
                   "jarak_geocoder_ke_osm_m": None, "rw_osm": None}
            if o:
                rec.update(osm_lat=o[0], osm_lon=o[1], osm_nama=o[2])
                if not pd.isna(r["lat_f"]):
                    rec["jarak_geocoder_ke_osm_m"] = jarak_m(o[0], o[1], r["lat_f"], r["lon_f"])
                hit = rt[rt.contains(Point(o[1], o[0]))]
                if len(hit):
                    rec["rw_osm"] = hit.iloc[0]["nmsls"]
            hasil.append(rec)
            bar.progress(n / len(rows))
            time.sleep(1.05)
        st.session_state["osm_batch"] = pd.DataFrame(hasil)
    if "osm_batch" in st.session_state:
        b = st.session_state["osm_batch"]
        found = b["osm_lat"].notna().sum()
        st.caption(f"Ditemukan OSM: {found} dari {len(b)} alamat. Alamat gang/RT "
                   "sering tidak ada di OSM, jadi 'tidak ditemukan' bukan berarti salah.")
        st.dataframe(b.drop(columns=["idx"]).sort_values("jarak_geocoder_ke_osm_m",
                                                          ascending=False, na_position="last"),
                     width="stretch", hide_index=True,
                     column_config={"jarak_geocoder_ke_osm_m": st.column_config.NumberColumn(
                         "Jarak geocoder ↔ OSM (m)", format="%.0f")})
        st.download_button("Unduh hasil cek OSM (CSV)",
                           b.to_csv(index=False).encode("utf-8-sig"),
                           "cek_osm_alamat_asli.csv", "text/csv")
